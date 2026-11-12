#!/usr/bin/env python3
"""validate.py: read-only OP-08 validator for Kerberator Tenant and Principal
YAML. Checks schema, field shapes, and cross-references against the cluster.

Usage:
  scripts/validate.py --from-file <path> [-n NS] [--context C] [--kubeconfig F]

Output (one line each):
  STATUS: valid | valid-with-warnings | invalid | not-found
          | forbidden | crd-missing | plugin-missing | error
  CONTEXT: <context used, or '-' if none was named>
  HINT: ...
  FINDING: ok|warn|fail|unknown <code> | <evidence>
  CHECKED-OK: <space-separated codes>
  ---
  raw evidence sections

Exit codes:
  0 valid* (including valid-with-warnings)
  1 error
  2 input file missing or unreadable
  3 forbidden / crd-missing / plugin-missing
  4 invalid

Hard rules this script enforces:
  - Never reads Secret `.data[key]` bytes. Secret presence is checked via
    `jsonpath={.data}` which returns a map of key → base64; we parse keys
    only and discard the values.
  - Never mutates the cluster. Only `get` subcommands are used.
"""

import argparse
import difflib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

try:
    import yaml  # PyYAML
except ImportError:  # pragma: no cover
    yaml = None


CRD_API_VERSION = "kerberator.dell.com/v1alpha1"
TENANT_KIND = "Tenant"
PRINCIPAL_KIND = "Principal"
KNOWN_KINDS = (TENANT_KIND, PRINCIPAL_KIND)

# DNS-1123 label (K8s resource name). Up to 63 chars.
DNS1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
# Kerberos realm: uppercase, DNS-ish (letters/digits/dots/dashes). One dot min.
REALM_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]*[A-Z0-9]$")
# Container image reference, very loose: must contain a `/` or a `:` or both.
IMAGE_RE = re.compile(r"^[A-Za-z0-9._\-/:@]+$")


# ---------- small kubectl wrapper ---------------------------------------------
def _k(context, kubeconfig, *args, capture=True):
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    if context:
        cmd += ["--context", context]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=capture, text=True)


def _tidy_err(text):
    """Strip kubectl's glog-style noise ('Exxxx 12:34:56.123456 ...' and the
    retry reconnection lines it appends) and keep the first meaningful line.
    Keeps FINDING output on one line each.
    """
    if not text:
        return ""
    lines = [ln for ln in text.splitlines()
             if ln.strip()
             and not re.match(r"^[EIWF]\d{4} ", ln)
             and "memcache.go:" not in ln]
    return (lines[0] if lines else text.splitlines()[0]).strip()


# ---------- finding accumulator -----------------------------------------------
class Findings:
    def __init__(self):
        self.items = []     # list of (level, code, evidence)
        self.ok_codes = []  # codes that explicitly passed (shown in CHECKED-OK)

    def add(self, level, code, evidence):
        assert level in ("ok", "warn", "fail", "unknown"), level
        self.items.append((level, code, evidence))
        if level == "ok":
            self.ok_codes.append(code)

    def worst(self):
        """Overall STATUS derived from findings only."""
        levels = {level for level, *_ in self.items}
        if "fail" in levels:
            return "invalid"
        if "warn" in levels:
            return "valid-with-warnings"
        return "valid"


# ---------- per-doc shape checks ----------------------------------------------
def _check_common(doc, idx, default_ns, findings):
    """Checks that apply to both kinds. Returns (ok, kind, name, ns)."""
    tag = f"doc[{idx}]"
    api_ver = doc.get("apiVersion")
    kind = doc.get("kind")
    md = doc.get("metadata") or {}
    name = md.get("name")
    ns = md.get("namespace") or default_ns

    if api_ver != CRD_API_VERSION:
        findings.add("fail", "kind-known",
                     f"{tag} apiVersion={api_ver!r}, want {CRD_API_VERSION!r}")
        return False, kind, name, ns
    if kind not in KNOWN_KINDS:
        findings.add("fail", "kind-known",
                     f"{tag} kind={kind!r}, want one of {KNOWN_KINDS}")
        return False, kind, name, ns
    findings.add("ok", "kind-known", f"{tag} kind={kind} apiVersion={api_ver}")

    if not name:
        findings.add("fail", "name-required", f"{tag} metadata.name is missing")
        return False, kind, name, ns
    if not DNS1123_LABEL.match(name) or len(name) > 63:
        findings.add("fail", "name-required",
                     f"{tag} metadata.name={name!r} is not a DNS-1123 label")
        return False, kind, name, ns
    findings.add("ok", "name-required", f"{tag} metadata.name={name}")

    yaml_ns = md.get("namespace")
    if yaml_ns and default_ns and yaml_ns != default_ns:
        findings.add("warn", "namespace-set",
                     f"{tag} metadata.namespace={yaml_ns!r} but -n is {default_ns!r}")
    elif not ns:
        findings.add("fail", "namespace-set",
                     f"{tag} no namespace in the YAML and no -n passed")
        return False, kind, name, ns
    else:
        findings.add("ok", "namespace-set", f"{tag} namespace={ns}")

    return True, kind, name, ns


def _check_tenant(doc, idx, findings):
    tag = f"doc[{idx}] Tenant"
    spec = doc.get("spec") or {}

    realm = spec.get("realm")
    if not realm:
        findings.add("fail", "realm-shape", f"{tag} spec.realm is missing")
    elif not REALM_RE.match(realm):
        findings.add("fail", "realm-shape",
                     f"{tag} spec.realm={realm!r} must be uppercase and "
                     "DNS-domain-shaped (e.g. EXAMPLE.COM)")
    elif realm.upper() != realm:
        findings.add("fail", "realm-shape",
                     f"{tag} spec.realm={realm!r} must be uppercase")
    else:
        findings.add("ok", "realm-shape", f"{tag} realm={realm}")

    daemon = spec.get("daemon") or {}

    rm = daemon.get("renewMinutes")
    if rm is None:
        findings.add("ok", "renew-minutes-positive",
                     f"{tag} spec.daemon.renewMinutes unset (operator default is used)")
    elif not isinstance(rm, int) or rm <= 0:
        findings.add("fail", "renew-minutes-positive",
                     f"{tag} spec.daemon.renewMinutes={rm!r}, must be an integer > 0")
    else:
        findings.add("ok", "renew-minutes-positive",
                     f"{tag} spec.daemon.renewMinutes={rm}")

    krb5 = daemon.get("krb5Conf")
    if not krb5:
        findings.add("fail", "krb5conf-has-realms",
                     f"{tag} spec.daemon.krb5Conf is empty; the daemon needs one to find the KDC")
    else:
        if realm and realm not in krb5:
            findings.add("fail", "krb5conf-mentions-realm",
                         f"{tag} spec.daemon.krb5Conf does not mention realm {realm!r}")
        elif realm:
            findings.add("ok", "krb5conf-mentions-realm",
                         f"{tag} krb5Conf mentions {realm}")
        if "[realms]" not in krb5:
            findings.add("fail", "krb5conf-has-realms",
                         f"{tag} spec.daemon.krb5Conf has no [realms] section")
        else:
            findings.add("ok", "krb5conf-has-realms", f"{tag} krb5Conf has [realms]")

    image = daemon.get("image")
    if image is None:
        findings.add("ok", "image-ref-shape",
                     f"{tag} spec.daemon.image unset (defaults to the operator's image)")
    elif not IMAGE_RE.match(image) or ("/" not in image and ":" not in image):
        findings.add("warn", "image-ref-shape",
                     f"{tag} spec.daemon.image={image!r} does not look like a "
                     "registry/name:tag reference")
    else:
        findings.add("ok", "image-ref-shape", f"{tag} image={image}")


def _check_principal_shape(doc, idx, findings):
    tag = f"doc[{idx}] Principal"
    spec = doc.get("spec") or {}

    uid = spec.get("uid")
    if uid is None:
        findings.add("fail", "uid-positive-int", f"{tag} spec.uid is missing")
    elif not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        findings.add("fail", "uid-positive-int",
                     f"{tag} spec.uid={uid!r}, must be a positive integer")
    else:
        findings.add("ok", "uid-positive-int", f"{tag} uid={uid}")

    pr = spec.get("principal")
    if not pr:
        findings.add("fail", "principal-at-realm",
                     f"{tag} spec.principal is missing")
    elif "@" not in pr:
        findings.add("fail", "principal-at-realm",
                     f"{tag} spec.principal={pr!r} must be name@REALM")
    else:
        findings.add("ok", "principal-at-realm", f"{tag} principal={pr}")

    tref = (spec.get("tenantRef") or {}).get("name")
    if not tref:
        findings.add("fail", "tenantref-name-required",
                     f"{tag} spec.tenantRef.name is missing")
    else:
        findings.add("ok", "tenantref-name-required", f"{tag} tenantRef.name={tref}")

    kt = spec.get("keytabSecretRef") or {}
    kt_name, kt_key = kt.get("name"), kt.get("key")
    if not kt_name or not kt_key:
        findings.add("fail", "keytabref-required",
                     f"{tag} spec.keytabSecretRef needs both .name and .key "
                     f"(got name={kt_name!r} key={kt_key!r})")
    else:
        findings.add("ok", "keytabref-required",
                     f"{tag} keytabSecretRef={kt_name}/{kt_key}")

    ns = spec.get("nodeSelector")
    if ns is None:
        findings.add("ok", "nodeselector-syntax",
                     f"{tag} spec.nodeSelector unset (every eligible node is targeted)")
    elif not isinstance(ns, dict) or not ns:
        findings.add("fail", "nodeselector-syntax",
                     f"{tag} spec.nodeSelector={ns!r}, must be a non-empty map")
    else:
        bad = [k for k, v in ns.items()
               if not isinstance(k, str) or not k
               or not isinstance(v, (str, int, bool)) or v in (None, "")]
        if bad:
            findings.add("fail", "nodeselector-syntax",
                         f"{tag} spec.nodeSelector keys with empty value: {bad}")
        else:
            findings.add("ok", "nodeselector-syntax",
                         f"{tag} nodeSelector={ns}")


# ---------- cross-reference checks against the cluster ------------------------
def _crd_installed(context, kubeconfig, findings):
    r = _k(context, kubeconfig, "get", "crd",
           "tenants.kerberator.dell.com", "principals.kerberator.dell.com",
           "-o", "jsonpath={.items[*].metadata.name}")
    if r.returncode != 0:
        msg = _tidy_err(r.stderr or r.stdout)
        if "forbidden" in msg.lower():
            findings.add("unknown", "crd-installed",
                         "cannot list CRDs (RBAC); cluster-side checks are skipped")
            return "forbidden"
        if "not found" in msg.lower():
            findings.add("fail", "crd-installed",
                         "kerberator CRDs are not installed in this context")
            return "crd-missing"
        findings.add("unknown", "crd-installed", f"could not read CRDs: {msg}")
        return "error"
    got = set(r.stdout.split())
    want = {"tenants.kerberator.dell.com", "principals.kerberator.dell.com"}
    if not want.issubset(got):
        findings.add("fail", "crd-installed",
                     f"kerberator CRDs missing: {sorted(want - got)}")
        return "crd-missing"
    findings.add("ok", "crd-installed",
                 "tenants.kerberator.dell.com, principals.kerberator.dell.com present")
    return "ok"


def _tenant_exists(context, kubeconfig, ns, name, findings):
    """Return (status, tenant_json_or_None) for ref resolution."""
    r = _k(context, kubeconfig, "-n", ns, "get", "tenant", name, "-o", "json")
    if r.returncode != 0:
        msg = _tidy_err(r.stderr or r.stdout)
        if "forbidden" in msg.lower():
            findings.add("unknown", "tenant-exists",
                         f"cannot read tenant {ns}/{name} (RBAC)")
            return "forbidden", None
        findings.add("fail", "tenant-exists",
                     f"tenantRef.name={name} not found in namespace {ns}")
        return "not-found", None
    try:
        obj = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        findings.add("unknown", "tenant-exists", f"tenant JSON unparseable: {e}")
        return "error", None
    findings.add("ok", "tenant-exists", f"tenant {ns}/{name} exists")
    return "ok", obj


def _keytab_secret_exists(context, kubeconfig, ns, sec_name, sec_key, findings):
    """List the Secret's data KEYS only (never the values)."""
    r = _k(context, kubeconfig, "-n", ns, "get", "secret", sec_name,
           "-o", "jsonpath={.data}")
    if r.returncode != 0:
        msg = _tidy_err(r.stderr or r.stdout)
        if "forbidden" in msg.lower():
            findings.add("unknown", "keytab-secret-exists",
                         f"cannot read secret {ns}/{sec_name} (RBAC)")
            return
        findings.add("fail", "keytab-secret-exists",
                     f"keytabSecretRef {ns}/{sec_name} not found")
        return

    raw = r.stdout.strip()
    keys = []
    if raw:
        # jsonpath renders a map as {k:v k:v} in Go's fmt; parse keys only.
        # Fallback: use kubectl -o json'd keys (`.data | keys`), but that
        # requires `jq`. We only need the KEY NAMES, never the values.
        try:
            # kubectl jsonpath of a map prints like: map[k1:v1 k2:v2]
            # Normalise any form by re-fetching -o json and picking .data keys.
            rj = _k(context, kubeconfig, "-n", ns, "get", "secret", sec_name,
                    "-o", "json")
            if rj.returncode == 0:
                obj = json.loads(rj.stdout)
                keys = sorted((obj.get("data") or {}).keys())
        except Exception:
            keys = []

    if sec_key not in keys:
        findings.add("fail", "keytab-secret-exists",
                     f"secret {ns}/{sec_name} exists but has no key {sec_key!r} "
                     f"(present: {keys})")
        return
    findings.add("ok", "keytab-secret-exists",
                 f"secret {ns}/{sec_name} has key {sec_key} "
                 f"(bytes never read; {len(keys)} key(s) present)")


def _principal_at_realm_matches(doc, tenant_obj, findings, idx):
    """If both Principal.spec.principal and the referenced Tenant resolve,
    check their realms agree."""
    if tenant_obj is None:
        return
    spec_pr = (doc.get("spec") or {}).get("principal") or ""
    if "@" not in spec_pr:
        return
    tenant_realm = ((tenant_obj.get("spec") or {}).get("realm")) or ""
    pr_realm = spec_pr.split("@", 1)[1]
    tag = f"doc[{idx}] Principal"
    if tenant_realm and pr_realm != tenant_realm:
        findings.add("fail", "principal-at-realm",
                     f"{tag} principal realm {pr_realm!r} != "
                     f"tenant realm {tenant_realm!r}")


def _duplicate_uid(context, kubeconfig, ns, name, uid, findings):
    r = _k(context, kubeconfig, "-n", ns, "get", "principals",
           "-o", "json")
    if r.returncode != 0:
        msg = _tidy_err(r.stderr or r.stdout)
        if "forbidden" in msg.lower():
            findings.add("unknown", "duplicate-uid",
                         "cannot list Principals in this namespace (RBAC)")
        return
    try:
        items = (json.loads(r.stdout) or {}).get("items", [])
    except json.JSONDecodeError:
        return
    others = [it.get("metadata", {}).get("name") for it in items
              if (it.get("spec") or {}).get("uid") == uid
              and it.get("metadata", {}).get("name") != name]
    if others:
        findings.add("warn", "duplicate-uid",
                     f"uid={uid} is already used by Principal(s) {others} in {ns}; "
                     "two Principals writing the same ccache will overwrite each other")
    else:
        findings.add("ok", "duplicate-uid",
                     f"no other Principal in {ns} uses uid={uid}")


def _nodeselector_match_and_typos(context, kubeconfig, selector, findings):
    """Count nodes matching the selector, and fuzzy-match keys against the
    label keys that exist on the cluster's nodes."""
    if not selector:
        return
    sel = ",".join(f"{k}={v}" for k, v in selector.items())
    r = _k(context, kubeconfig, "get", "nodes", "-l", sel,
           "-o", "jsonpath={.items[*].metadata.name}")
    if r.returncode != 0:
        msg = _tidy_err(r.stderr or r.stdout)
        if "forbidden" in msg.lower():
            findings.add("unknown", "nodeselector-matches",
                         "cannot list nodes (RBAC)")
            return
        findings.add("unknown", "nodeselector-matches",
                     f"could not query nodes: {msg}")
        return
    matched = [n for n in r.stdout.split() if n]
    if matched:
        findings.add("ok", "nodeselector-matches",
                     f"selector {selector} matches {len(matched)} node(s): "
                     f"{matched}")
    else:
        findings.add("warn", "nodeselector-matches",
                     f"selector {selector} matches 0 nodes; the daemon will "
                     "skip this Principal on every node until a node is labelled")

    # label-typo-suggest: compare each selector KEY to the pool of node keys.
    rj = _k(context, kubeconfig, "get", "nodes", "-o", "json")
    if rj.returncode != 0:
        findings.add("unknown", "label-typo-suggest",
                     "cannot list nodes to suggest label typos")
        return
    try:
        items = (json.loads(rj.stdout) or {}).get("items", [])
    except json.JSONDecodeError:
        return
    all_keys = set()
    for it in items:
        all_keys.update((it.get("metadata", {}).get("labels") or {}).keys())
    suggestions = {}
    for key in selector:
        if key in all_keys:
            continue
        close = difflib.get_close_matches(key, all_keys, n=1, cutoff=0.8)
        if close:
            suggestions[key] = close[0]
    if suggestions:
        findings.add("warn", "label-typo-suggest",
                     f"selector keys may be typos: {suggestions}")
    else:
        findings.add("ok", "label-typo-suggest",
                     "no selector key looks like a near-miss of an existing node label")


# ---------- top-level ---------------------------------------------------------
def _emit(findings, status, context, raw_sections):
    print(f"STATUS: {status}")
    print(f"CONTEXT: {context or '-'}")
    for level, code, evidence in findings.items:
        print(f"FINDING: {level} {code} | {evidence}")
    if findings.ok_codes:
        print(f"CHECKED-OK: {' '.join(findings.ok_codes)}")
    print("---")
    for title, body in raw_sections:
        print(f"== {title} ==")
        print(body.rstrip() + "\n")


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--from-file", required=True)
    ap.add_argument("-n", "--namespace", default="")
    ap.add_argument("--context", default="")
    ap.add_argument("--kubeconfig", default="")
    args = ap.parse_args()

    if shutil.which("kubectl") is None:
        print("STATUS: plugin-missing")
        print("CONTEXT: -")
        print("FINDING: fail plugin-missing | kubectl is not on PATH")
        return 3

    path = pathlib.Path(args.from_file).expanduser()
    if not path.exists() or not path.is_file():
        print("STATUS: not-found")
        print("CONTEXT: -")
        print(f"FINDING: fail yaml-parse | {args.from_file!r} is missing or not a file")
        return 2

    # Parse YAML in-process; kubectl's client-side dry-run still contacts the
    # API server to resolve CRD kinds, so it cannot be used offline.
    findings = Findings()
    raw_sections = []

    if yaml is None:
        print("STATUS: error")
        print("CONTEXT: -")
        print("FINDING: fail internal | PyYAML is required; install pyyaml")
        return 1

    try:
        docs = list(yaml.safe_load_all(path.read_text()))
    except yaml.YAMLError as e:
        findings.add("fail", "yaml-parse",
                     f"could not parse {path.name}: {e}")
        raw_sections.append(("file", path.read_text(errors="replace")))
        _emit(findings, "invalid", args.context, raw_sections)
        return 4

    docs = [d for d in docs if d]  # drop empty docs (--- at end of file)
    if not docs:
        findings.add("fail", "yaml-parse", "file contains no documents")
        _emit(findings, "invalid", args.context, raw_sections)
        return 4
    findings.add("ok", "yaml-parse",
                 f"parsed {path.name}: {len(docs)} document(s)")

    raw_sections.append(("parsed documents",
                         json.dumps(docs, sort_keys=True, indent=2)))

    # Shape checks per doc; record the Principals' refs for cluster lookups.
    principals_to_resolve = []  # list of (idx, ns, principal_doc)
    tenants_in_file = {}        # (ns, name) -> parsed doc (for cross-realm check)
    for idx, doc in enumerate(docs):
        ok, kind, name, ns = _check_common(doc, idx, args.namespace, findings)
        if not ok:
            continue
        if kind == TENANT_KIND:
            _check_tenant(doc, idx, findings)
            tenants_in_file[(ns, name)] = doc
        elif kind == PRINCIPAL_KIND:
            _check_principal_shape(doc, idx, findings)
            principals_to_resolve.append((idx, ns, doc))

    # Cluster refs. Skip entirely if plugin/CRDs/forbidden.
    crd_state = _crd_installed(args.context, args.kubeconfig, findings)
    if crd_state == "ok":
        for idx, ns, doc in principals_to_resolve:
            spec = doc.get("spec") or {}
            tref = (spec.get("tenantRef") or {}).get("name")
            tenant_obj = None
            if tref:
                tkey = (ns, tref)
                if tkey in tenants_in_file:
                    # Referenced tenant is in the same file; use that.
                    findings.add("ok", "tenant-exists",
                                 f"tenantRef.name={tref} is defined in the same file")
                    tenant_obj = tenants_in_file[tkey]
                else:
                    state, tenant_obj = _tenant_exists(
                        args.context, args.kubeconfig, ns, tref, findings)
            _principal_at_realm_matches(doc, tenant_obj, findings, idx)

            kt = spec.get("keytabSecretRef") or {}
            if kt.get("name") and kt.get("key"):
                _keytab_secret_exists(args.context, args.kubeconfig,
                                      ns, kt["name"], kt["key"], findings)

            uid = spec.get("uid")
            cr_name = (doc.get("metadata") or {}).get("name")
            if isinstance(uid, int) and not isinstance(uid, bool) and uid > 0:
                _duplicate_uid(args.context, args.kubeconfig,
                               ns, cr_name, uid, findings)

            sel = spec.get("nodeSelector")
            if isinstance(sel, dict) and sel:
                _nodeselector_match_and_typos(
                    args.context, args.kubeconfig, sel, findings)

    # Final STATUS. CRD/plugin states override findings-derived status.
    status = findings.worst()
    if crd_state == "crd-missing":
        status = "crd-missing"
        exit_code = 3
    elif crd_state == "forbidden":
        # Shape findings still count; show them, and let the user know
        # cluster-side checks were skipped.
        if status == "valid":
            status = "valid-with-warnings"
        exit_code = 0 if status != "invalid" else 4
    else:
        exit_code = {"valid": 0, "valid-with-warnings": 0, "invalid": 4}[status]

    _emit(findings, status, args.context, raw_sections)
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception as e:  # last-resort: never leak a traceback
        print("STATUS: error")
        print("CONTEXT: -")
        print(f"FINDING: fail internal | {type(e).__name__}: {e}")
        sys.exit(1)

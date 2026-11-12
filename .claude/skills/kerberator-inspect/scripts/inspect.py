#!/usr/bin/env python3
# inspect.py — read-only wrapper over `kubectl krb list|describe`.
#
#   inspect.py contexts [--kubeconfig F]
#   inspect.py list [-A | -n NS] [--tenant NAME] [--context C] [--kubeconfig F]
#   inspect.py describe <tenant|principal> <name> [-n NS] [--context C] [--kubeconfig F]
#
# `contexts` lists the clusters (kubeconfig contexts) available; it reads the
# local kubeconfig only and contacts no cluster. Pass --context to target one.
#
# Line 1 is always `STATUS: <state>`, line 2 `CONTEXT: <cluster context used>`,
# then optional `HINT:` lines, then `---`, then the raw CLI output. States:
#   ok | empty | not-found | not-found-in-ns(<ns>[,<ns>...]) |
#   not-found-by-name | not-found-unverified | forbidden | crd-missing | plugin-missing | error
# Exit codes: 0 ok/empty, 2 not-found* (incl. -by-name, -unverified), 3 forbidden/crd-missing/plugin-missing, 1 error.
#
# This script only ever invokes `kubectl krb list`, `kubectl krb describe`,
# and the local, read-only `kubectl config get-contexts|view|current-context`.

import os
import re
import shutil
import subprocess
import sys

USAGE = """\
inspect.py — read-only wrapper over `kubectl krb list|describe`.

  inspect.py contexts [--kubeconfig F]
  inspect.py list [-A | -n NS] [--tenant NAME] [--context C] [--kubeconfig F]
  inspect.py describe <tenant|principal> <name> [-n NS] [--context C] [--kubeconfig F]

`contexts` lists the clusters (kubeconfig contexts) available; it reads the
local kubeconfig only and contacts no cluster. Pass --context to target one.

Line 1 is always `STATUS: <state>`, line 2 `CONTEXT: <cluster context used>`,
then optional `HINT:` lines, then `---`, then the raw CLI output. States:
  ok | empty | not-found | not-found-in-ns(<ns>[,<ns>...]) |
  not-found-by-name | not-found-unverified | forbidden | crd-missing | plugin-missing | error
Exit codes: 0 ok/empty, 2 not-found* (incl. -by-name, -unverified), 3 forbidden/crd-missing/plugin-missing, 1 error.

This script only ever invokes `kubectl krb list`, `kubectl krb describe`,
and the local, read-only `kubectl config get-contexts|view|current-context`.
"""


def die_usage():
    sys.stderr.write(USAGE)
    sys.exit(64)


# Argument parsing. The first word is the subcommand; the rest are sorted into
# buckets because different calls need different subsets: `kubectl krb
# describe` gets the namespace, the fallback `list -A` must not, and
# `kubectl config ...` accepts only --kubeconfig.
if len(sys.argv) < 2:
    die_usage()

sub = sys.argv[1]
argv = sys.argv[2:]

common = []      # global kubectl-style flags passed through
kcfg = []        # --kubeconfig F only (for `kubectl config ...`)
ctx_flag = ""
ns_flags = []
all_ns = False
tenant_filter = ""
pos = []

i = 0
while i < len(argv):
    a = argv[i]
    if a == "--context":
        if i + 1 >= len(argv):
            sys.stderr.write(f"{a} needs a value\n"); sys.exit(64)
        ctx_flag = argv[i + 1]
        common += [a, argv[i + 1]]
        i += 2
    elif a == "--kubeconfig":
        if i + 1 >= len(argv):
            sys.stderr.write(f"{a} needs a value\n"); sys.exit(64)
        kcfg = [a, argv[i + 1]]
        common += [a, argv[i + 1]]
        i += 2
    elif a in ("-n", "--namespace"):
        if i + 1 >= len(argv):
            sys.stderr.write(f"{a} needs a value\n"); sys.exit(64)
        ns_flags += ["-n", argv[i + 1]]
        i += 2
    elif a in ("-A", "--all-namespaces"):
        all_ns = True
        ns_flags += ["-A"]
        i += 1
    elif a == "--tenant":
        if i + 1 >= len(argv):
            sys.stderr.write(f"--tenant needs a value\n"); sys.exit(64)
        tenant_filter = argv[i + 1]
        i += 2
    elif a in ("-h", "--help"):
        die_usage()
    else:
        pos.append(a)
        i += 1


# Run `kubectl krb` with the same --context/--kubeconfig on every call, so
# all lookups in one run hit the same cluster. Returns (stdout+stderr, rc).
def krb(*args):
    r = subprocess.run(["kubectl", "krb", *args, *common],
                       capture_output=True, text=True)
    return (r.stdout + r.stderr).rstrip("\n"), r.returncode


def _run_config(*args):
    r = subprocess.run(["kubectl", "config", *args, *kcfg],
                       capture_output=True, text=True)
    return (r.stdout + r.stderr).rstrip("\n"), r.returncode


# The context every cluster call in this run is aimed at.
def ctx_name():
    if ctx_flag:
        return ctx_flag
    r = subprocess.run(["kubectl", "config", "current-context", *kcfg],
                       capture_output=True, text=True)
    c = r.stdout.strip()
    return c or "<none set>"


_EXIT_BY_STATUS = {"ok": 0, "empty": 0,
                   "forbidden": 3, "crd-missing": 3, "plugin-missing": 3}


def emit(status, raw, *hints):
    """Print the fixed format and exit with the status's code."""
    print(f"STATUS: {status}")
    print(f"CONTEXT: {ctx_name()}")
    for h in hints:
        print(f"HINT: {h}")
    print("---")
    if raw:
        print(raw)
    if status in _EXIT_BY_STATUS:
        sys.exit(_EXIT_BY_STATUS[status])
    if status.startswith("not-found"):
        sys.exit(2)
    sys.exit(1)


# Map raw kubectl error text to a status. Order matters: forbidden is checked
# first so a permissions failure is never reported as "doesn't exist".
def classify_error(raw):
    if re.search(r"forbidden|cannot (list|get) resource", raw, re.I):
        return "forbidden"
    if re.search(r"no matches for kind|could not find the requested resource|doesn.t have a resource type", raw, re.I):
        return "crd-missing"
    if re.search(r"not found", raw, re.I):
        return "not-found"
    return "error"


# kubectl / plugin pre-flight.
if not shutil.which("kubectl"):
    emit("plugin-missing", "kubectl is not on PATH",
         "install kubectl, then kubectl-krb (see cli/kubectl-krb/README.md)")


# `contexts`: which clusters can be targeted. Local kubeconfig read only.
# Runs before the plugin check because it never talks to a cluster.
if sub == "contexts":
    names_out, rc = _run_config("get-contexts", "-o", "name")
    if rc != 0:
        emit("error", names_out, "could not read kubeconfig")
    names = names_out
    n = sum(1 for ln in names.splitlines() if ln) if names else 0
    table, _ = _run_config("get-contexts")
    r = subprocess.run(
        ["kubectl", "config", "view", *kcfg,
         "-o", r'jsonpath={range .clusters[*]}{.name}{"  "}{.cluster.server}{"\n"}{end}'],
        capture_output=True, text=True)
    servers = r.stdout
    raw = f"{table}\n\nCluster API servers:\n{servers}"
    if n == 0:
        emit("empty", raw,
             "contexts=0: no cluster is configured; ask the user for a kubeconfig or context")
    elif n == 1:
        emit("ok", raw,
             "contexts=1: only one cluster is configured; use it and say which")
    else:
        emit("ok", raw,
             f"contexts={n}: several clusters are configured; if the user did not name one, ask which to inspect (one, some, or all)")


# Everything below calls the cluster through the plugin, so make sure it works.
_v = subprocess.run(["kubectl", "krb", "version", *common],
                    capture_output=True, text=True)
if _v.returncode != 0:
    emit("plugin-missing", "kubectl krb version failed",
         "build/install the plugin: go build -o kubectl-krb ./cli/kubectl-krb/cmd/kubectl-krb")


# Cluster-wide listing, fetched once. ALL_RC != 0 means other namespaces could
# not be searched (e.g. RBAC forbids `list -A`), which is *not* the same as
# "no match elsewhere".
_all_cache = {"out": None, "rc": None}


def load_all():
    if _all_cache["rc"] is not None:
        return
    out, rc = krb("list", "-A")
    _all_cache["out"] = out
    _all_cache["rc"] = rc


# Search every namespace for a resource of kind (Tenant|Principal) matching
# name. Needs load_all first. Returns a sorted list of unique "name NS/CRNAME"
# and (for Principals with UID match) "uid NS/CRNAME" entries.
def find_elsewhere(kind, name):
    if _all_cache["rc"] != 0:
        return []
    results = set()
    for line in (_all_cache["out"] or "").splitlines():
        f = line.split()
        if len(f) < 3:
            continue
        # fields: NAMESPACE KIND NAME TENANT/UID ...
        if f[1] == kind and f[2] == name:
            results.add(f"name {f[0]}/{f[2]}")
        if (f[1] == kind and kind == "Principal"
                and len(f) >= 4 and f[3] == name and f[2] != name):
            results.add(f"uid {f[0]}/{f[2]}")
    return sorted(results)


def emit_unverified(raw, what):
    why = classify_error(_all_cache["out"] or "")
    emit("not-found-unverified", raw,
         f"{what} not found in the queried namespace; could NOT search other namespaces (list -A: {why})",
         "absence elsewhere is unknown; try the namespace you expect with -n, or ask for broader access")


# list and work out the results.
if sub == "list":
    # list: ok | empty, or, for `--tenant X` without -A, a cluster-wide search
    # that turns "nothing here" into not-found / not-found-in-ns / -unverified.
    args = ["list", *ns_flags]
    if tenant_filter:
        args += ["--tenant", tenant_filter]
    raw, rc = krb(*args)
    if rc != 0:
        emit(classify_error(raw), raw)
    if "No Kerberator resources found" in raw:
        hints = []
        if tenant_filter and not all_ns:
            load_all()
            if _all_cache["rc"] != 0:
                emit_unverified(raw, f"tenant '{tenant_filter}'")
            matches = find_elsewhere("Tenant", tenant_filter)
            by_name = [m[len("name "):] for m in matches if m.startswith("name ")]
            if by_name:
                nss = ",".join(dict.fromkeys(m.split("/", 1)[0] for m in by_name))
                emit(f"not-found-in-ns({nss})", raw,
                     f"tenant '{tenant_filter}' exists in: {nss} (re-run with -n <ns>)")
            emit("not-found", raw,
                 f"no tenant '{tenant_filter}' in any namespace")
        elif not all_ns:
            hints.append("namespace may be wrong; re-run with -A before concluding the cluster is empty")
        emit("empty", raw, *hints)
    emit("ok", raw)


if sub == "describe":
    # describe: on success, ok plus hints for warning markers in the output.
    # On not-found, search all namespaces to explain the miss.
    if len(pos) != 2:
        die_usage()
    kind_arg = pos[0]
    if kind_arg in ("tenant", "tenants", "kt"):
        kind, arg = "Tenant", "tenant"
    elif kind_arg in ("principal", "principals", "kp"):
        kind, arg = "Principal", "principal"
    else:
        sys.stderr.write(f"unknown kind '{kind_arg}' (want tenant|principal)\n")
        sys.exit(64)
    name = pos[1]
    raw, rc = krb("describe", arg, name, *ns_flags)
    if rc == 0:
        hints = []
        if "<not observed>" in raw:
            hints.append("KVNO not observed: keytab Secret missing, empty, or unparseable")
        if "Deleting:" in raw:
            hints.append("resource is being deleted (deletionTimestamp set)")
        if re.search(r"\(none — ", raw):
            hints.append("no recent daemon Events: not reported yet, or GC'd")
        emit("ok", raw, *hints)
    status = classify_error(raw)
    if status == "not-found":
        load_all()
        if _all_cache["rc"] != 0:
            emit_unverified(raw, f"{kind} '{name}'")
        matches = find_elsewhere(kind, name)
        by_name = [m[len("name "):] for m in matches if m.startswith("name ")]
        by_uid = [m[len("uid "):] for m in matches if m.startswith("uid ")]
        if by_name:
            nss = ",".join(sorted({m.split("/", 1)[0] for m in by_name}))
            emit(f"not-found-in-ns({nss})", raw,
                 f"{kind} '{name}' exists as: {','.join(by_name)} (re-run with -n <ns>)")
        elif by_uid:
            emit("not-found-by-name", raw,
                 f"'{name}' is a UID, not a CR name; Principal(s) with UID {name}: {','.join(by_uid)} (ns/CR-name; re-run: describe principal <CR-name> -n <ns>)")
        hints = [f"no {kind} matching '{name}' in any namespace"]
        if kind == "Principal":
            hints.append("Principal CR names are not UIDs; run 'list -A' to see real names and UIDs")
        emit("not-found", raw, *hints)
    # forbidden / crd-missing / error: pass through unchanged.
    emit(status, raw)


die_usage()

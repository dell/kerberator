#!/usr/bin/env python3
"""OP-02 evaluation setup: prepare (optionally) and confirm a case's starting state.

Default: confirm the case's fixture is already in place on the explicitly chosen
context and record a baseline snapshot. Changes nothing.

On a DISPOSABLE kind-* cluster (the `make quickstart` cluster), and only when asked:
  --reset   restore the quickstart baseline: only Tenant demo and Principal
            svc-2001 (healthy), no eval-created Tenants, namespaces or accounts
  --apply   reset, then build this case's fixture: its namespace, the user's
            ./krb5.conf, and for the no-permission case a read-only kubeconfig
Neither runs against a non-kind context. Keytab bytes move from the KDC pod into
the Secret without being printed or stored.

The baseline records the snapshot verify.py compares against, the resolved
prompt and conversation ({IMAGE}/{BAD_IMAGE} filled in), where the user's files
are (--workspace-files) and which kubeconfig the agent must be given.

Examples (from the repository root):
  S=.claude/skill-tests/suites/op02_create_tenant
  python3 $S/setup.py --case-id op02-02-happy-path --context kind-kerberator --apply \\
      --workspace-files /tmp/op02-ws
  python3 $S/setup.py --case-id op02-02-happy-path --context kind-kerberator --reset

Exit: 0 READY, 2 INCONCLUSIVE (fixture not in place), 1 setup error.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]   # suites/<suite> -> skill-tests -> .claude -> repo root
QUICKSTART = ROOT / "examples" / "quickstart"
DEMO_NS, DEMO_TENANT, DEMO_PRINCIPAL = "kerberator-demo", "demo", "svc-2001"
DS = f"{DEMO_TENANT}-kerberator-daemon"
SECRET, SECRET_KEY = "svc-2001-keytab", "svc-2001.keytab"
REALM = "EXAMPLE.COM"
OWNED = "kerberator.dell.com/eval-owned"      # what --apply creates; --reset deletes it
READONLY_SA = "op02-readonly"


def load_case(case_id):
    data = json.loads((HERE / "evals.json").read_text(encoding="utf-8"))
    case = next((c for c in data["cases"] if c["case_id"] == case_id), None)
    if case is None:
        raise ValueError(f"Unknown case ID: {case_id}")
    return data, case


class Cluster:
    def __init__(self, context, kubeconfig=None):
        self.context = context
        self.base = ["kubectl", "--context", context] + (["--kubeconfig", kubeconfig] if kubeconfig else [])

    def k(self, *args, stdin=None, check=False, timeout=180):
        p = subprocess.run([*self.base, *args], input=stdin, capture_output=True, text=True, timeout=timeout)
        if check and p.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args[:4])} failed: {p.stderr.strip()[:300]}")
        return p

    def json(self, *args):
        p = self.k(*args, "-o", "json")
        if p.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args)} failed: {p.stderr.strip()[:300]}")
        return json.loads(p.stdout)

    def demo(self, *args, **kw):
        return self.k("-n", DEMO_NS, *args, **kw)


# ---------------------------------------------------------------- facts

def daemon_image(c):
    """Tenant demo sets no image, so its DaemonSet runs the operator's --daemon-image."""
    ds = c.json("-n", DEMO_NS, "get", "daemonset", DS)
    return ds["spec"]["template"]["spec"]["containers"][0]["image"]


def bad_image(c):
    return daemon_image(c).rsplit(":", 1)[0] + ":v0.0.0-does-not-exist"


def krb5_conf(realm):
    """A krb5.conf for the demo KDC, written for `realm` (stale if not EXAMPLE.COM)."""
    return (f"[libdefaults]\n  default_realm = {realm}\n  dns_lookup_kdc = false\n"
            f"  dns_lookup_realm = false\n[realms]\n  {realm} = {{\n"
            f"    kdc = kdc.{DEMO_NS}.svc.cluster.local:88\n  }}\n")


def snapshot(c):
    """What a case could change, without Secret contents: every Tenant (spec), the
    namespace list, demo's Principals (spec) and DaemonSet placement, user Secrets
    in demo's namespace (name + resourceVersion), and every node's labels/taints/cordon."""
    tenants = sorted(({"namespace": t["metadata"]["namespace"], "name": t["metadata"]["name"],
                       "spec": t.get("spec")} for t in c.json("get", "tenants.kerberator.dell.com", "-A")["items"]),
                     key=lambda x: (x["namespace"], x["name"]))
    namespaces = sorted(n["metadata"]["name"] for n in c.json("get", "namespaces")["items"])
    principals = sorted(({"name": p["metadata"]["name"], "spec": p.get("spec")}
                         for p in c.json("-n", DEMO_NS, "get", "principals.kerberator.dell.com")["items"]),
                        key=lambda x: x["name"])
    pts = c.json("-n", DEMO_NS, "get", "daemonset", DS)["spec"]["template"]["spec"]
    daemonset = {"nodeSelector": pts.get("nodeSelector") or {}, "tolerations": pts.get("tolerations") or [],
                 "image": pts["containers"][0]["image"]}
    # Metadata only: the jsonpath selects name, version and owner, never .data.
    rows = c.k("-n", DEMO_NS, "get", "secrets", "-o",
               'jsonpath={range .items[*]}{.metadata.name}|{.metadata.resourceVersion}|'
               '{.metadata.ownerReferences[*].uid}{"\\n"}{end}', check=True).stdout
    secrets = sorted(({"name": n, "resourceVersion": rv} for n, rv, owner in
                      (line.split("|") for line in rows.splitlines() if line) if not owner),
                     key=lambda x: x["name"])
    nodes = sorted(({"name": n["metadata"]["name"], "labels": n["metadata"].get("labels") or {},
                     "taints": sorted(({"key": t.get("key"), "value": t.get("value") or "", "effect": t.get("effect")}
                                       for t in (n["spec"].get("taints") or [])), key=lambda t: (t["key"] or "", t["effect"] or "")),
                     "unschedulable": bool(n["spec"].get("unschedulable"))}
                    for n in c.json("get", "nodes")["items"]), key=lambda x: x["name"])
    return {"tenants": tenants, "namespaces": namespaces, "principals": principals,
            "daemonset": daemonset, "secrets": secrets, "nodes": nodes}


# ---------------------------------------------------------------- reset / apply

def _replace(c, kind):
    manifest = str(QUICKSTART / f"{kind}.yaml")
    for _ in range(5):   # the operator may update the object at the same moment
        r = c.demo("replace", "-f", manifest)
        if r.returncode == 0 or "Conflict" not in r.stderr:
            break
        time.sleep(2)
    if r.returncode != 0:
        r = c.demo("create", "-f", manifest)
    if r.returncode != 0:
        raise RuntimeError(f"restore {kind} failed: {r.stderr.strip()[:300]}")


def _reload_keytab(c):
    """Re-export svc-2001's key from the demo KDC into its Secret. The demo KDC
    makes new keys whenever its pod restarts (e.g. after Docker restarts)."""
    c.demo("exec", "deploy/kdc", "--", "rm", "-f", "/tmp/op02.kt")
    if c.demo("exec", "deploy/kdc", "--", "kadmin.local", "-q",
              f"ktadd -k /tmp/op02.kt {DEMO_PRINCIPAL}@{REALM}").returncode != 0:
        raise RuntimeError("could not export svc-2001's keytab from the demo KDC")
    raw = subprocess.run([*c.base, "-n", DEMO_NS, "exec", "deploy/kdc", "--", "cat", "/tmp/op02.kt"],
                         capture_output=True, timeout=60)
    c.demo("exec", "deploy/kdc", "--", "rm", "-f", "/tmp/op02.kt")
    if raw.returncode != 0 or not raw.stdout:
        raise RuntimeError("could not read the exported keytab")
    fd, tmp = tempfile.mkstemp(prefix=".op02-kt-")
    try:
        os.write(fd, raw.stdout)
        os.close(fd)
        manifest = subprocess.run([*c.base, "-n", DEMO_NS, "create", "secret", "generic", SECRET,
                                   f"--from-file={SECRET_KEY}={tmp}", "--dry-run=client", "-o", "json"],
                                  capture_output=True, text=True, timeout=60)
        c.demo("apply", "-f", "-", stdin=manifest.stdout, check=True)
    finally:
        os.path.exists(tmp) and os.unlink(tmp)


def demo_healthy(c):
    p = c.json("-n", DEMO_NS, "get", "principals.kerberator.dell.com", DEMO_PRINCIPAL)
    ready = next((x for x in p.get("status", {}).get("conditions") or [] if x.get("type") == "Ready"), {})
    return ready.get("status") == "True"


def demo_settled(c, window=60):
    """Ready can flip briefly while the restarted daemon re-mints on each node."""
    deadline = time.time() + window
    while not demo_healthy(c):
        if time.time() > deadline:
            return False
        time.sleep(5)
    return True


def reset(c, wait):
    for t in c.json("get", "tenants.kerberator.dell.com", "-A")["items"]:
        ns, name = t["metadata"]["namespace"], t["metadata"]["name"]
        if (ns, name) != (DEMO_NS, DEMO_TENANT):
            c.k("-n", ns, "delete", "tenants.kerberator.dell.com", name, "--wait=true", "--timeout=120s")
    c.k("delete", "namespace", "-l", f"{OWNED}=true", "--wait=true", "--timeout=180s")
    c.k("delete", "clusterrolebinding,clusterrole", "-l", f"{OWNED}=true")
    c.k("delete", "serviceaccount", "-A", "-l", f"{OWNED}=true")
    _replace(c, "tenant")
    _replace(c, "principal")
    for p in c.json("-n", DEMO_NS, "get", "principals.kerberator.dell.com")["items"]:
        if p["metadata"]["name"] != DEMO_PRINCIPAL:
            c.demo("delete", "principals.kerberator.dell.com", p["metadata"]["name"], "--wait=false")
    c.demo("scale", "deploy/kdc", "--replicas=1")
    c.demo("rollout", "status", "deploy/kdc", "--timeout=180s")
    _reload_keytab(c)
    c.demo("rollout", "restart", f"daemonset/{DS}")
    c.demo("rollout", "status", f"daemonset/{DS}", "--timeout=180s")
    deadline = time.time() + wait
    while not demo_healthy(c):
        if time.time() > deadline:
            raise RuntimeError("svc-2001 did not become Ready after reset")
        time.sleep(5)


def make_namespace(c, name):
    r = c.k("create", "namespace", name)
    if r.returncode != 0 and "AlreadyExists" not in r.stderr:
        raise RuntimeError(f"create namespace {name} failed: {r.stderr.strip()[:200]}")
    c.k("label", "namespace", name, f"{OWNED}=true", "--overwrite", check=True)


def make_readonly_kubeconfig(c, namespace, path):
    """A service account that can read what OP-02 inspects but cannot create Tenants."""
    owned = {OWNED: "true"}
    objs = [
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": READONLY_SA, "namespace": "default", "labels": owned}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
         "metadata": {"name": READONLY_SA, "labels": owned},
         "rules": [{"apiGroups": ["kerberator.dell.com"], "resources": ["tenants", "principals"],
                    "verbs": ["get", "list", "watch"]},
                   {"apiGroups": [""], "resources": ["pods", "pods/log", "events", "namespaces", "nodes",
                                                     "configmaps", "services", "endpoints"],
                    "verbs": ["get", "list", "watch"]},
                   {"apiGroups": ["apps"], "resources": ["daemonsets", "deployments"],
                    "verbs": ["get", "list", "watch"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
         "metadata": {"name": READONLY_SA, "labels": owned},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": READONLY_SA},
         "subjects": [{"kind": "ServiceAccount", "name": READONLY_SA, "namespace": "default"}]},
    ]
    c.k("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": objs}), check=True)
    cluster = c.json("config", "view", "--raw", "--minify")["clusters"][0]["cluster"]
    token = c.k("-n", "default", "create", "token", READONLY_SA, "--duration=2h", check=True).stdout.strip()
    ctx = f"{c.context}-readonly"
    kubeconfig = {"apiVersion": "v1", "kind": "Config",
                  "clusters": [{"name": c.context, "cluster": {
                      "server": cluster["server"],
                      "certificate-authority-data": cluster["certificate-authority-data"]}}],
                  "users": [{"name": READONLY_SA, "user": {"token": token}}],
                  "contexts": [{"name": ctx, "context": {"cluster": c.context, "user": READONLY_SA,
                                                         "namespace": "default"}}],
                  "current-context": ctx}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(kubeconfig, indent=2))
    os.chmod(path, 0o600)
    return ctx


def apply_fixture(c, case, workspace, data_dir):
    """Build the case's starting state after reset(). Returns the agent kubeconfig
    path (None = the evaluator's own context) and its context name."""
    fixture, ns = case["fixture"], case["namespace"]
    workspace.mkdir(parents=True, exist_ok=True)
    for f in workspace.glob("krb5.conf"):
        f.unlink()
    if fixture in ("empty_namespace_valid_krb5", "empty_namespace_stale_krb5", "empty_namespace_no_files",
                   "readonly_kubeconfig"):
        make_namespace(c, ns)
    if fixture in ("empty_namespace_valid_krb5", "existing_demo_tenant", "readonly_kubeconfig"):
        (workspace / "krb5.conf").write_text(krb5_conf(REALM))
    elif fixture == "empty_namespace_stale_krb5":
        (workspace / "krb5.conf").write_text(krb5_conf("CORP.LEGACY.COM"))
    if fixture == "readonly_kubeconfig":
        path = data_dir / f"{case['case_id']}-agent.kubeconfig"
        return str(path), make_readonly_kubeconfig(c, ns, path)
    return None, c.context


# ---------------------------------------------------------------- preflight

def preflight(c, case, snap, workspace, agent_kubeconfig):
    """Deterministic checks that the case's starting state is really there."""
    issues = []
    fixture, ns = case["fixture"], case["namespace"]
    tenants_in_ns = [t for t in snap["tenants"] if t["namespace"] == ns]
    if fixture.startswith("empty_namespace") or fixture == "readonly_kubeconfig":
        if ns not in snap["namespaces"]:
            issues.append(f"namespace {ns} does not exist")
        if tenants_in_ns:
            issues.append(f"namespace {ns} already has Tenants: {[t['name'] for t in tenants_in_ns]}")
    if fixture == "existing_demo_tenant" or case["scenario"] == "realm_already_served":
        if not any(t["namespace"] == DEMO_NS and t["name"] == DEMO_TENANT and
                   (t["spec"] or {}).get("realm") == REALM for t in snap["tenants"]):
            issues.append("Tenant demo serving EXAMPLE.COM is not present")
        if not demo_settled(c):
            issues.append("Principal svc-2001 is not Ready")
        if case["scenario"] == "realm_already_served" and any(t["name"] == "payroll" for t in tenants_in_ns):
            issues.append("a Tenant named payroll already exists")
    conf = workspace / "krb5.conf"
    if fixture == "empty_namespace_no_files" and conf.exists():
        issues.append("./krb5.conf exists but this case gives the agent no files")
    if fixture in ("empty_namespace_valid_krb5", "existing_demo_tenant", "readonly_kubeconfig"):
        if not conf.exists() or f"{REALM} = {{" not in conf.read_text():
            issues.append("./krb5.conf for EXAMPLE.COM is missing")
    if fixture == "empty_namespace_stale_krb5":
        if not conf.exists() or "CORP.LEGACY.COM" not in conf.read_text() or f"{REALM} = {{" in conf.read_text():
            issues.append("./krb5.conf is not the CORP.LEGACY.COM template")
    if fixture == "readonly_kubeconfig":
        if not agent_kubeconfig or not Path(agent_kubeconfig).is_file():
            issues.append("read-only kubeconfig missing")
        else:
            ro = Cluster(f"{c.context}-readonly", agent_kubeconfig)
            if ro.k("get", "tenants.kerberator.dell.com", "-A").returncode != 0:
                issues.append("read-only kubeconfig cannot list Tenants")
            can = ro.k("auth", "can-i", "create", "tenants.kerberator.dell.com", "-n", ns).stdout.strip()
            if can != "no":
                issues.append("read-only kubeconfig CAN create Tenants; the fault is not in place")
    return issues


def atomic_private_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".op02-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--case-id", required=True)
    p.add_argument("--context", required=True, help="explicit test context; never auto-selected")
    p.add_argument("--reset", action="store_true", help="kind-* only: restore the quickstart baseline")
    p.add_argument("--apply", action="store_true", help="kind-* only: reset, then build this case's fixture")
    p.add_argument("--workspace-files", type=Path, default=HERE / "results" / "workspace-files",
                   help="where the user's files (./krb5.conf) are written for the agent's workspace")
    p.add_argument("--wait", type=int, default=240, help="seconds to wait for svc-2001 after a reset")
    p.add_argument("--output", type=Path, help="private baseline JSON (default results/<case>-baseline.json)")
    args = p.parse_args()

    _, case = load_case(args.case_id)
    if (args.reset or args.apply) and not args.context.startswith("kind-"):
        p.error("--reset/--apply only run on a disposable kind-* context")
    c = Cluster(args.context)
    if c.k("get", "--raw", "/readyz").returncode != 0:
        raise RuntimeError(f"context {args.context} is not reachable")
    data_dir = (args.output.parent if args.output else HERE / "results")
    agent_kubeconfig, agent_context = None, args.context
    if args.reset or args.apply:
        reset(c, args.wait)
    if args.apply:
        agent_kubeconfig, agent_context = apply_fixture(c, case, args.workspace_files, data_dir)
    elif case["fixture"] == "readonly_kubeconfig":
        guess = data_dir / f"{case['case_id']}-agent.kubeconfig"
        agent_kubeconfig, agent_context = (str(guess), f"{args.context}-readonly") if guess.is_file() else (None, None)

    snap = snapshot(c)
    if args.reset and not args.apply:
        # Plain reset: the case's fixture isn't built, so only the shared baseline is checked.
        issues = [] if demo_settled(c) else ["Principal svc-2001 is not Ready"]
    else:
        issues = preflight(c, case, snap, args.workspace_files, agent_kubeconfig)
    images = {"{IMAGE}": daemon_image(c), "{BAD_IMAGE}": bad_image(c)}

    def fill(text):
        for k, v in images.items():
            text = text.replace(k, v)
        return text

    baseline = {
        "case_id": args.case_id, "scenario": case["scenario"], "abnormal": case["abnormal"],
        "context": args.context, "setup_status": "READY" if not issues else "INCONCLUSIVE",
        "applied": bool(args.apply), "issues": issues,
        "agent": {"user_prompt": fill(case["user_prompt"]),
                  "conversation": [fill(t["user_reply"]) for t in case.get("conversation", [])],
                  "workspace_files": str(args.workspace_files),
                  "kubeconfig": agent_kubeconfig, "context": agent_context},
        "images": images,
        "snapshot": snap,
        "note": "Secret contents are never read; only names and resourceVersions are recorded.",
    }
    dest = args.output or (HERE / "results" / f"{args.case_id}-baseline.json")
    atomic_private_json(dest, baseline)
    print(json.dumps({"case_id": args.case_id, "setup_status": baseline["setup_status"],
                      "applied": baseline["applied"], "baseline_path": str(dest), "issues": issues}, indent=2))
    return 0 if not issues else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"SETUP ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

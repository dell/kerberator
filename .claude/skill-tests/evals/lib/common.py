""".claude/skill-tests/evals/lib/common.py: shared helpers for eval setup/verify scripts.

Import it; do not run it. Environment (all optional):

  EVAL_CLUSTER     kind cluster name                  (default kerberator-eval)
  EVAL_KUBECONFIG  kubeconfig holding ONLY that cluster (default $EVAL_STATE/kubeconfig,
                   written by setup/cluster.py); every kubectl call uses it, so the
                   scripts can never touch another cluster in ~/.kube/config
  EVAL_CONTEXT     context inside EVAL_KUBECONFIG     (default kind-$EVAL_CLUSTER)
  EVAL_NS          namespace of the demo Tenant/Principal (default kerberator-demo)
  EVAL_STATE       local scratch dir for the kubeconfig and baseline snapshot
                   (default ${TMPDIR:-/tmp}/kerberator-eval)
  EVAL_TIMEOUT     seconds to wait for a condition    (default 300)
"""

import json
import os
import pathlib
import subprocess
import sys
import time


LIB_DIR = pathlib.Path(__file__).resolve().parent
EVALS_DIR = LIB_DIR.parent
REPO_ROOT = EVALS_DIR.parents[2]

EVAL_CLUSTER = os.environ.get("EVAL_CLUSTER") or "kerberator-eval"
EVAL_STATE = (os.environ.get("EVAL_STATE")
              or f"{os.environ.get('TMPDIR', '/tmp').rstrip('/')}/kerberator-eval")
EVAL_STATE = EVAL_STATE.rstrip("/")
EVAL_KUBECONFIG = os.environ.get("EVAL_KUBECONFIG") or f"{EVAL_STATE}/kubeconfig"
EVAL_CONTEXT = os.environ.get("EVAL_CONTEXT") or f"kind-{EVAL_CLUSTER}"
EVAL_NS = os.environ.get("EVAL_NS") or "kerberator-demo"
EVAL_TIMEOUT = int(os.environ.get("EVAL_TIMEOUT") or 300)

TENANT = "demo"
PRINCIPAL = "svc-2001"
DS = f"{TENANT}-kerberator-daemon"
DAEMON_PODS = f"app.kubernetes.io/name=kerberator-daemon,app.kubernetes.io/instance={TENANT}"

# kind node names: <cluster>-control-plane, <cluster>-worker, <cluster>-worker2.
CP = f"{EVAL_CLUSTER}-control-plane"
W1 = f"{EVAL_CLUSTER}-worker"
W2 = f"{EVAL_CLUSTER}-worker2"

# Label and taint keys the evals may set. reset.py removes exactly these,
# so it never strips a label it did not own.
EVAL_LABEL_KEYS = ["security-zone", "security_zone", "gpu-tier",
                   "kerberator", "env", "a", "b", "zone"]
EVAL_TAINT_KEYS = ["dedicated"]

BASELINE = f"{EVAL_STATE}/baseline.json"

# Objects an eval creates outside the demo namespace (namespaces, service
# accounts, cluster roles) carry this label. reset.py deletes everything with
# it, so it never deletes something it did not create.
EVAL_OWNED_LABEL = "kerberator.dell.com/eval-owned"

# Files the user "has" in the agent's working directory (e.g. ./krb5.conf).
# Setup writes them here; run.py copies them into the agent's workspace.
CASE_FILES = f"{EVAL_STATE}/case-files"

os.makedirs(EVAL_STATE, exist_ok=True)


# ----- logging / control flow -----
def log(msg):
    script = os.path.basename(sys.argv[0]) if sys.argv else "common.py"
    sys.stderr.write(f"[{script}] {msg}\n")


def die(msg):
    log(f"ERROR: {msg}")
    sys.exit(1)


def skip(msg):
    log(f"SKIP: {msg}")
    sys.exit(77)


# ----- kubectl wrappers -----
def k(*args, check=False):
    """kubectl with --kubeconfig / --context. Returns CompletedProcess."""
    cmd = ["kubectl", "--kubeconfig", EVAL_KUBECONFIG, "--context", EVAL_CONTEXT, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def kn(*args, check=False):
    """kubectl -n EVAL_NS ..."""
    return k("-n", EVAL_NS, *args, check=check)


def k_json(*args):
    """kubectl ... -o json; returns parsed JSON (dict/list) or raises."""
    r = k(*args, "-o", "json")
    if r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def kn_json(*args):
    r = kn(*args, "-o", "json")
    if r.returncode != 0:
        raise RuntimeError(f"kubectl -n {EVAL_NS} {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


# ----- node name resolution -----
def node(alias):
    """@cp/@w1/@w2 -> real node name; anything else is returned as-is."""
    return {"@cp": CP, "@w1": W1, "@w2": W2}.get(alias, alias)


def nodes_csv(csv):
    """Resolve aliases in a comma-separated list, sort (no dedup), comma-join."""
    parts = [node(n) for n in csv.split(",") if n]
    parts = [p for p in parts if p]
    return ",".join(sorted(parts))


# ----- wait -----
def wait_for(what, fn, *args, timeout=None):
    """Call fn(*args) every 3s until it returns truthy or timeout passes.
    Exceptions during fn are treated as 'not yet'."""
    t = timeout if timeout is not None else EVAL_TIMEOUT
    deadline = time.time() + t
    while True:
        try:
            if fn(*args):
                log(f"ok: {what}")
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            log(f"timed out after {t}s waiting for: {what}")
            return False
        time.sleep(3)


# ----- preconditions -----
def require_cluster():
    kc = pathlib.Path(EVAL_KUBECONFIG)
    if not kc.exists() or kc.stat().st_size == 0:
        die(f"no kubeconfig at {EVAL_KUBECONFIG}; run setup/cluster.py first")
    r = k("get", "--raw", "/readyz")
    if r.returncode != 0:
        die(f"cluster {EVAL_CONTEXT} is not reachable (is Docker running?)")


def require_workers():
    """OP-06 needs two schedulable workers with the kind names."""
    r1 = k("get", "node", W1); r2 = k("get", "node", W2)
    if r1.returncode != 0 or r2.returncode != 0:
        skip(f"needs nodes {W1} and {W2} (a kind cluster from setup/kind-eval.yaml)")


# ----- introspection -----
def principal_json(name=None):
    return kn_json("get", "principal", name or PRINCIPAL)


def minted_nodes(name=None):
    """Sorted comma list of nodes whose status reason is Minted."""
    p = principal_json(name)
    names = sorted(n["name"] for n in (p.get("status", {}).get("nodes") or [])
                   if n.get("reason") == "Minted")
    return ",".join(names)


def daemon_nodes():
    """Sorted comma list of nodes running a Ready daemon pod."""
    po = kn_json("get", "pods", "-l", DAEMON_PODS)
    out = []
    for pod in po.get("items", []):
        if (pod.get("metadata", {}) or {}).get("deletionTimestamp"):
            continue
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in (pod.get("status", {}).get("conditions") or []))
        if ready:
            nn = (pod.get("spec", {}) or {}).get("nodeName")
            if nn:
                out.append(nn)
    return ",".join(sorted(out))


def nodes_labelled(selector, csv):
    """Exactly the nodes in `csv` match `selector` ('' means none do)."""
    r = k("get", "nodes", "-l", selector,
          "-o", "jsonpath={.items[*].metadata.name}")
    if r.returncode != 0:
        return False
    names = sorted(n for n in r.stdout.split() if n)
    return ",".join(names) == csv


def daemon_on(csv):
    return daemon_nodes() == csv


# ----- ccache (ground truth read via docker exec) -----
def _all_nodes():
    r = k("get", "nodes", "-o", "jsonpath={.items[*].metadata.name}")
    if r.returncode != 0:
        return []
    return [n for n in r.stdout.split() if n]


def ccache_nodes(name=None):
    """Sorted comma list of nodes that hold the Principal's ticket file.

    Reads the file's existence via docker exec, so it does not depend on
    the operator's status reporting.
    """
    p = principal_json(name)
    uid = str(p["spec"]["uid"])
    r = kn("get", "tenant", TENANT, "-o", "jsonpath={.spec.daemon.hostCachePath}")
    hdir = r.stdout.strip() or "/tmp"
    out = []
    for n in _all_nodes():
        d = subprocess.run(["docker", "exec", n, "test", "-e", f"{hdir}/krb5cc_{uid}"],
                           capture_output=True, text=True)
        if d.returncode == 0:
            out.append(n)
    return ",".join(sorted(out))


def ccache_on(csv, name=None):
    """The ticket file exists on every node in csv, and on no other node that
    runs a daemon. (A node without a daemon can keep an old file nobody prunes;
    that is not a targeting error.)"""
    have_list = ccache_nodes(name).split(",") if ccache_nodes(name) else []
    have = set(have_list)
    daemons = daemon_nodes().split(",") if daemon_nodes() else []
    want = [n for n in csv.split(",") if n]
    for n in want:
        if n not in have:
            return False
    for n in daemons:
        if n not in want and n in have:
            return False
    return True


def principal_settled(name, minted_csv, ready_summary):
    """Status shows exactly those nodes Minted, Ready=True, and that n/n."""
    try:
        j = principal_json(name)
    except Exception:
        return False
    nodes = sorted(n["name"] for n in (j.get("status", {}).get("nodes") or [])
                   if n.get("reason") == "Minted")
    if ",".join(nodes) != minted_csv:
        return False
    rs = (j.get("status", {}).get("tenant", {}) or {}).get("readySummary", "") or ""
    if rs != ready_summary:
        return False
    for c in (j.get("status", {}).get("conditions") or []):
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return False


# ----- snapshot / baseline -----
def _canon_nodes(items):
    out = []
    for n in items:
        md = n.get("metadata", {}) or {}
        sp = n.get("spec", {}) or {}
        taints = [
            {"key": t.get("key"),
             "value": t.get("value") or "",
             "effect": t.get("effect")}
            for t in (sp.get("taints") or [])
        ]
        taints.sort(key=lambda x: (x["key"] or "", x["effect"] or ""))
        out.append({
            "name": md.get("name"),
            "labels": md.get("labels") or {},
            "taints": taints,
            "unschedulable": bool(sp.get("unschedulable", False)),
        })
    out.sort(key=lambda x: x["name"] or "")
    return out


def snapshot():
    """State that evals compare before/after, as a dict:
      - Tenant and Principal specs
      - DaemonSet pod-template placement
      - user-owned Secrets: name + resourceVersion only (no bytes read)
      - every node's labels, taints, and cordon flag
    """
    tenant = kn_json("get", "tenant", TENANT)["spec"]
    pr_items = kn_json("get", "principals").get("items", [])
    principals = sorted(
        [{"name": it.get("metadata", {}).get("name"), "spec": it.get("spec")}
         for it in pr_items],
        key=lambda x: x["name"] or "",
    )
    ds = kn_json("get", "daemonset", DS)
    pts = (ds.get("spec", {}).get("template", {}).get("spec", {}) or {})
    daemonset = {
        "nodeSelector": pts.get("nodeSelector") or {},
        "tolerations": pts.get("tolerations") or [],
        "image": pts["containers"][0]["image"],
    }
    sec_items = kn_json("get", "secrets").get("items", [])
    secrets = sorted(
        [{"name": it.get("metadata", {}).get("name"),
          "resourceVersion": it.get("metadata", {}).get("resourceVersion")}
         for it in sec_items
         if not ((it.get("metadata", {}) or {}).get("ownerReferences") or [])],
        key=lambda x: x["name"] or "",
    )
    node_items = k_json("get", "nodes").get("items", [])
    nodes = _canon_nodes(node_items)
    # Every Tenant in every namespace, so an eval where the agent must not
    # create one fails if it does (the `tenant` key above is only `demo`).
    tenants = sorted(
        [{"namespace": it["metadata"]["namespace"], "name": it["metadata"]["name"],
          "spec": it.get("spec")}
         for it in k_json("get", "tenants", "-A").get("items", [])],
        key=lambda x: (x["namespace"], x["name"]),
    )
    namespaces = sorted(it["metadata"]["name"]
                        for it in k_json("get", "namespaces").get("items", []))
    return {"tenant": tenant, "principals": principals,
            "daemonset": daemonset, "secrets": secrets, "nodes": nodes,
            "tenants": tenants, "namespaces": namespaces}


def save_baseline():
    try:
        snap = snapshot()
    except Exception as e:
        die(f"could not snapshot the cluster: {e}")
    tmp = f"{BASELINE}.tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f, sort_keys=True, indent=2)
    os.replace(tmp, BASELINE)
    log(f"baseline saved to {BASELINE}")


def read_baseline():
    with open(BASELINE) as f:
        return json.load(f)


# ----- label mutation -----
def label_node(name, *kvs):
    nn = node(name)
    r = k("label", "node", nn, *kvs, "--overwrite")
    if r.returncode != 0:
        die(f"label node {nn} {kvs} failed: {r.stderr.strip()}")


def unlabel_node(name, *keys):
    nn = node(name)
    for key in keys:
        k("label", "node", nn, f"{key}-")  # ignore failure


# ----- cordon / daemon-pod-per-node (OP-07) -----
def node_is_cordoned(name):
    """True iff node.spec.unschedulable is true."""
    nn = node(name)
    r = k("get", "node", nn, "-o", "jsonpath={.spec.unschedulable}")
    if r.returncode != 0:
        return False
    return r.stdout.strip() == "true"


def daemon_pod_on_node(name):
    """Return the Ready kerberator-daemon pod name on `name`, or None."""
    nn = node(name)
    po = kn_json("get", "pods", "-l", DAEMON_PODS)
    for pod in po.get("items", []):
        md = pod.get("metadata", {}) or {}
        sp = pod.get("spec", {}) or {}
        if md.get("deletionTimestamp"):
            continue
        if sp.get("nodeName") != nn:
            continue
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in (pod.get("status", {}).get("conditions") or []))
        if ready:
            return md.get("name")
    return None


# ----- Tenant creation (OP-02) -----
def daemon_image():
    """The daemon image the operator was installed with: Tenant demo sets no
    image, so its DaemonSet runs the operator's --daemon-image default."""
    ds = kn_json("get", "daemonset", DS)
    return ds["spec"]["template"]["spec"]["containers"][0]["image"]


def bad_image():
    """Same repository as daemon_image(), with a tag that does not exist."""
    return daemon_image().rsplit(":", 1)[0] + ":v0.0.0-does-not-exist"


def resolve_image(token):
    """@daemon-image / @bad-image -> the real image; anything else as-is."""
    return {"@daemon-image": daemon_image, "@bad-image": bad_image}.get(
        token, lambda: token)()


def krb5_conf(realm):
    """A krb5.conf for the demo KDC, written for `realm`. With a realm other
    than EXAMPLE.COM it is a stale template: the KDC serves EXAMPLE.COM."""
    return (f"[libdefaults]\n"
            f"  default_realm = {realm}\n"
            f"  dns_lookup_kdc = false\n"
            f"  dns_lookup_realm = false\n"
            f"[realms]\n"
            f"  {realm} = {{\n"
            f"    kdc = kdc.{EVAL_NS}.svc.cluster.local:88\n"
            f"  }}\n")


def write_case_file(name, text):
    """Put a file in the agent's working directory (copied in by run.py)."""
    os.makedirs(CASE_FILES, exist_ok=True)
    pathlib.Path(CASE_FILES, name).write_text(text)


def make_namespace(name):
    """Create a namespace that reset.py will delete again."""
    r = k("create", "namespace", name)
    if r.returncode != 0 and "AlreadyExists" not in r.stderr:
        die(f"create namespace {name} failed: {r.stderr.strip()}")
    k("label", "namespace", name, f"{EVAL_OWNED_LABEL}=true", "--overwrite", check=False)


def tenant_json(ns, name):
    return k_json("-n", ns, "get", "tenant", name)


def condition(obj, ctype):
    for c in (obj.get("status", {}) or {}).get("conditions") or []:
        if c.get("type") == ctype:
            return c.get("status") or ""
    return ""

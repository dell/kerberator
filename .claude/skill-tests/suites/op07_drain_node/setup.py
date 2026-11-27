#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""OP-07 trusted fixture preparation and baseline snapshot.

Only the evaluator runs this file; never expose it or evals.json to the agent.

Each scenario maps to a fixture function that resets the cluster to baseline
and then applies scenario-specific state (cordoning a node, creating extra
pods, PDBs, etc.).  The output is a trusted JSON baseline for verify.py.

Usage:
  python setup.py --scenario op07-01 --output /tmp/op07-01-baseline.json
  python setup.py --scenario op07-06 --output /tmp/op07-06-baseline.json
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

# Shared contract modules live in .claude/skill-tests/ (two levels up).
_SKILL_TESTS = Path(__file__).resolve().parents[2]
if str(_SKILL_TESTS) not in sys.path:
    sys.path.insert(0, str(_SKILL_TESTS))
from harness import environment as envlib  # noqa: E402


class SetupError(Exception):
    pass


EVAL_CLUSTER = os.environ.get("EVAL_CLUSTER") or "kerberator-eval"
EVAL_STATE = (os.environ.get("EVAL_STATE")
              or f"{os.environ.get('TMPDIR', '/tmp').rstrip('/')}/kerberator-eval")
EVAL_KUBECONFIG = os.environ.get("EVAL_KUBECONFIG") or f"{EVAL_STATE}/kubeconfig"
EVAL_CONTEXT = os.environ.get("EVAL_CONTEXT") or f"kind-{EVAL_CLUSTER}"
EVAL_NS = os.environ.get("EVAL_NS") or "kerberator-demo"
EVAL_TIMEOUT = int(os.environ.get("EVAL_TIMEOUT") or 300)

TENANT = "demo"
PRINCIPAL = "svc-2001"
DS = f"{TENANT}-kerberator-daemon"
DAEMON_PODS = f"app.kubernetes.io/name=kerberator-daemon,app.kubernetes.io/instance={TENANT}"

CP = f"{EVAL_CLUSTER}-control-plane"
W1 = f"{EVAL_CLUSTER}-worker"
W2 = f"{EVAL_CLUSTER}-worker2"

EVAL_LABEL_KEYS = ["security-zone", "security_zone", "gpu-tier",
                   "kerberator", "env", "a", "b", "zone"]
EVAL_TAINT_KEYS = ["dedicated"]
EVAL_OWNED_LABEL = "kerberator.dell.com/eval-owned"

REPO_ROOT = Path(__file__).resolve().parents[4]   # repo root (examples/quickstart lives there)

os.makedirs(EVAL_STATE, exist_ok=True)


def log(msg: str) -> None:
    sys.stderr.write(f"[setup.py] {msg}\n")


def die(msg: str) -> None:
    raise SetupError(msg)


def kubectl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    cmd = ["kubectl", "--kubeconfig", EVAL_KUBECONFIG, "--context", EVAL_CONTEXT, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def kn(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return kubectl("-n", EVAL_NS, *args, check=check)


def kubectl_json(*args: str) -> Any:
    r = kubectl(*args, "-o", "json")
    if r.returncode != 0:
        raise SetupError(f"kubectl {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def kn_json(*args: str) -> Any:
    r = kn(*args, "-o", "json")
    if r.returncode != 0:
        raise SetupError(f"kubectl -n {EVAL_NS} {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def wait_for(what: str, fn, *args, timeout: int | None = None) -> bool:
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


def require_cluster() -> None:
    kc = Path(EVAL_KUBECONFIG)
    if not kc.exists() or kc.stat().st_size == 0:
        die(f"no kubeconfig at {EVAL_KUBECONFIG}; run cluster.py first")
    r = kubectl("get", "--raw", "/readyz")
    if r.returncode != 0:
        die(f"cluster {EVAL_CONTEXT} is not reachable")


def require_workers() -> None:
    r1 = kubectl("get", "node", W1)
    r2 = kubectl("get", "node", W2)
    if r1.returncode != 0 or r2.returncode != 0:
        die(f"needs nodes {W1} and {W2}")


def node_is_cordoned(name: str) -> bool:
    r = kubectl("get", "node", name, "-o", "jsonpath={.spec.unschedulable}")
    return r.returncode == 0 and r.stdout.strip() == "true"


def daemon_pod_on_node(name: str) -> str | None:
    po = kn_json("get", "pods", "-l", DAEMON_PODS)
    for pod in po.get("items", []):
        md = pod.get("metadata", {}) or {}
        if md.get("deletionTimestamp"):
            continue
        if (pod.get("spec", {}) or {}).get("nodeName") != name:
            continue
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in (pod.get("status", {}).get("conditions") or []))
        if ready:
            return md.get("name")
    return None


def daemon_nodes() -> str:
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


def daemon_on(csv: str) -> bool:
    return daemon_nodes() == csv


def daemon_pod_uids() -> dict[str, str]:
    po = kn_json("get", "pods", "-l", DAEMON_PODS)
    out = {}
    for pod in po.get("items", []):
        md = pod.get("metadata", {}) or {}
        if md.get("deletionTimestamp"):
            continue
        nn = (pod.get("spec", {}) or {}).get("nodeName")
        uid = md.get("uid")
        if nn and uid:
            out[nn] = uid
    return out


def daemon_all_nodes() -> str:
    po = kn_json("get", "pods", "-l", DAEMON_PODS)
    out = []
    for pod in po.get("items", []):
        if (pod.get("metadata", {}) or {}).get("deletionTimestamp"):
            continue
        nn = (pod.get("spec", {}) or {}).get("nodeName")
        if nn:
            out.append(nn)
    return ",".join(sorted(set(out)))


def ccache_nodes(name: str | None = None) -> str:
    p = kn_json("get", "principal", name or PRINCIPAL)
    uid = str(p["spec"]["uid"])
    r = kn("get", "tenant", TENANT, "-o", "jsonpath={.spec.daemon.hostCachePath}")
    hdir = r.stdout.strip() or "/tmp"
    out = []
    all_nodes_r = kubectl("get", "nodes", "-o", "jsonpath={.items[*].metadata.name}")
    for n in (all_nodes_r.stdout.split() if all_nodes_r.returncode == 0 else []):
        d = subprocess.run(["docker", "exec", n, "test", "-e", f"{hdir}/krb5cc_{uid}"],
                           capture_output=True, text=True)
        if d.returncode == 0:
            out.append(n)
    return ",".join(sorted(out))


def ccache_on(csv: str, name: str | None = None) -> bool:
    have = set(ccache_nodes(name).split(",")) if ccache_nodes(name) else set()
    all_daemon = daemon_all_nodes().split(",") if daemon_all_nodes() else []
    want = [n for n in csv.split(",") if n]
    for n in want:
        if n not in have:
            return False
    for n in all_daemon:
        if n not in want and n in have:
            return False
    return True


def principal_json(name: str | None = None) -> dict:
    return kn_json("get", "principal", name or PRINCIPAL)


def minted_nodes(name: str | None = None) -> str:
    p = principal_json(name)
    names = sorted(n["name"] for n in (p.get("status", {}).get("nodes") or [])
                   if n.get("reason") == "Minted")
    return ",".join(names)


def principal_settled(name: str, minted_csv: str, ready_summary: str) -> bool:
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


def _canon_nodes(items: list) -> list:
    out = []
    for n in items:
        md = n.get("metadata", {}) or {}
        sp = n.get("spec", {}) or {}
        taints = [{"key": t.get("key"), "value": t.get("value") or "",
                   "effect": t.get("effect")}
                  for t in (sp.get("taints") or [])]
        taints.sort(key=lambda x: (x["key"] or "", x["effect"] or ""))
        out.append({"name": md.get("name"), "labels": md.get("labels") or {},
                    "taints": taints,
                    "unschedulable": bool(sp.get("unschedulable", False))})
    out.sort(key=lambda x: x["name"] or "")
    return out


def snapshot() -> dict:
    tenant = kn_json("get", "tenant", TENANT)["spec"]
    pr_items = kn_json("get", "principals").get("items", [])
    principals = sorted(
        [{"name": it.get("metadata", {}).get("name"), "spec": it.get("spec")}
         for it in pr_items],
        key=lambda x: x["name"] or "")
    ds = kn_json("get", "daemonset", DS)
    pts = (ds.get("spec", {}).get("template", {}).get("spec", {}) or {})
    daemonset = {"nodeSelector": pts.get("nodeSelector") or {},
                 "tolerations": pts.get("tolerations") or [],
                 "image": pts["containers"][0]["image"]}
    sec_items = kn_json("get", "secrets").get("items", [])
    secrets = sorted(
        [{"name": it.get("metadata", {}).get("name"),
          "resourceVersion": it.get("metadata", {}).get("resourceVersion")}
         for it in sec_items
         if not ((it.get("metadata", {}) or {}).get("ownerReferences") or [])],
        key=lambda x: x["name"] or "")
    node_items = kubectl_json("get", "nodes").get("items", [])
    nodes = _canon_nodes(node_items)
    wp_items = kubectl_json("get", "pods", "-n", "default").get("items", [])
    workload_pods = sorted(
        [{"name": it.get("metadata", {}).get("name"), "namespace": "default",
          "uid": it.get("metadata", {}).get("uid"),
          "nodeName": (it.get("spec", {}) or {}).get("nodeName")}
         for it in wp_items],
        key=lambda x: x["name"] or "")
    tenants = sorted(
        [{"namespace": it["metadata"]["namespace"], "name": it["metadata"]["name"],
          "spec": it.get("spec")}
         for it in kubectl_json("get", "tenants", "-A").get("items", [])],
        key=lambda x: (x["namespace"], x["name"]))
    namespaces = sorted(it["metadata"]["name"]
                        for it in kubectl_json("get", "namespaces").get("items", []))
    return {"tenant": tenant, "principals": principals, "daemonset": daemonset,
            "secrets": secrets, "nodes": nodes, "workload_pods": workload_pods,
            "daemon_pod_uids": daemon_pod_uids(), "tenants": tenants,
            "namespaces": namespaces}


def save_baseline(output: Path) -> dict:
    snap = snapshot()
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(json.dumps(snap, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, output)
    log(f"baseline saved to {output}")
    return snap


# ---------------------------------------------------------------------------
# Reset: bring cluster back to clean baseline state
# ---------------------------------------------------------------------------
def reset() -> None:
    for n in (W1, W2):
        kubectl("uncordon", n)

    for pod in ("nginx-test", "nginx-pdb-test", "emptydir-test"):
        kubectl("delete", "pod", pod, "-n", "default", "--ignore-not-found", "--wait=false")
    kubectl("delete", "pdb", "nginx-pdb", "-n", "default", "--ignore-not-found", "--wait=false")

    for f in ("tenant", "principal"):
        manifest = REPO_ROOT / "examples" / "quickstart" / f"{f}.yaml"
        for _ in range(5):
            if kn("get", "-f", str(manifest)).returncode == 0:
                r = kn("replace", "-f", str(manifest))
            else:
                r = kn("create", "-f", str(manifest))
            if r.returncode == 0 or "Conflict" not in r.stderr:
                break
            time.sleep(2)
        if r.returncode != 0:
            die(f"apply {manifest} failed: {r.stderr.strip()}")

    for p in [it["metadata"]["name"]
              for it in kn_json("get", "principals").get("items", [])]:
        if p and p != PRINCIPAL:
            kn("delete", "principal", p, "--wait=false")

    try:
        all_tenants = kubectl_json("get", "tenants", "-A").get("items", [])
    except Exception as e:
        die(f"could not list Tenants: {e}")
    for t in all_tenants:
        ns, name = t["metadata"]["namespace"], t["metadata"]["name"]
        if (ns, name) != (EVAL_NS, TENANT):
            r = kubectl("-n", ns, "delete", "tenant", name, "--wait=true", "--timeout=120s")
            if r.returncode != 0 and "NotFound" not in r.stderr:
                die(f"delete tenant {ns}/{name} failed: {r.stderr.strip()}")
    owned = f"{EVAL_OWNED_LABEL}=true"
    kubectl("delete", "namespace", "-l", owned, "--wait=true", "--timeout=180s")
    kubectl("delete", "clusterrolebinding,clusterrole", "-l", owned)
    kubectl("delete", "serviceaccount", "-A", "-l", owned)

    kt_key = f"{PRINCIPAL}.keytab"
    kdc_env = "true"
    cmd = (f"{kdc_env}; rm -f /tmp/cur.kt; "
           f"kadmin.local -q 'ktadd -k /tmp/cur.kt -norandkey {PRINCIPAL}@EXAMPLE.COM' "
           f">/dev/null 2>&1 && klist -k -K /tmp/cur.kt | tail -n +4 | sort | sha256sum")
    kdc_pod = f"kdc-kerberator-kdc-0"
    r = kn("exec", kdc_pod, "--", "bash", "-c", cmd)
    if r.returncode != 0:
        die(f"cannot read KDC keytab hash: {r.stderr.strip()}")
    kdc_hash = r.stdout.strip().split()[0]
    r = kn("get", "secret", f"{PRINCIPAL}-keytab", "-o",
           f"jsonpath={{.data.{kt_key}}}")
    if r.returncode != 0 or not r.stdout:
        die(f"cannot read k8s keytab secret: {r.stderr.strip()}")
    secret_b64 = r.stdout
    verify_cmd = (f"cat /dev/stdin > /tmp/sec.kt && "
                  f"klist -k -K /tmp/sec.kt | tail -n +4 | sort | sha256sum")
    r2 = subprocess.run(
        ["kubectl", "--kubeconfig", EVAL_KUBECONFIG, "--context", EVAL_CONTEXT,
         "-n", EVAL_NS, "exec", "-i", kdc_pod, "--", "bash", "-c", verify_cmd],
        input=base64.b64decode(secret_b64), capture_output=True)
    sec_hash = r2.stdout.decode().strip().split()[0] if r2.returncode == 0 else ""
    if sec_hash != kdc_hash:
        log("keytab hash mismatch — re-exporting from KDC")
        export_cmd = (f"{kdc_env}; rm -f /tmp/new.kt; "
                      f"kadmin.local -q 'ktadd -k /tmp/new.kt -norandkey {PRINCIPAL}@EXAMPLE.COM' "
                      f">/dev/null 2>&1 && base64 /tmp/new.kt")
        r3 = kn("exec", kdc_pod, "--", "bash", "-c", export_cmd)
        if r3.returncode != 0 or not r3.stdout.strip():
            die(f"keytab re-export failed: {r3.stderr.strip()}")
        new_b64 = r3.stdout.strip()
        patch = json.dumps({"data": {kt_key: new_b64}})
        r4 = kn("patch", "secret", f"{PRINCIPAL}-keytab", "-p", patch)
        if r4.returncode != 0:
            die(f"keytab secret patch failed: {r4.stderr.strip()}")

    both = f"{W1},{W2}"
    if not wait_for(f"daemon Ready on {both}", daemon_on, both):
        die("daemon pods did not reach Ready on both workers")
    if not wait_for(f"{PRINCIPAL} Minted 2/2", principal_settled, PRINCIPAL, both, "2/2"):
        die(f"{PRINCIPAL} did not reach 2/2 Minted")
    if not wait_for(f"ticket files on {both}", ccache_on, both):
        die("ticket files not present on both workers")
    log("reset complete")


# ---------------------------------------------------------------------------
# Fixture functions
# ---------------------------------------------------------------------------
def fixture_baseline() -> None:
    reset()


def fixture_drained() -> None:
    reset()
    r = kubectl("cordon", W1)
    if r.returncode != 0:
        die(f"cordon {W1} failed: {r.stderr.strip()}")
    if not wait_for(f"{W1} cordoned", node_is_cordoned, W1):
        die(f"{W1} did not report unschedulable")
    r = kn("delete", "pod", "-l", DAEMON_PODS,
           "--field-selector", f"spec.nodeName={W1}", "--wait=true")
    if r.returncode != 0:
        die(f"delete daemon pod on {W1} failed: {r.stderr.strip()}")
    if not wait_for(f"daemon pod gone on {W1}",
                    lambda: daemon_pod_on_node(W1) is None):
        die(f"daemon pod on {W1} did not terminate")
    if not wait_for(f"daemon Ready on {W2} only", daemon_on, W2):
        die(f"daemon not Ready on {W2}")
    if not wait_for(f"{PRINCIPAL} Minted on {W2} (1/1)",
                    principal_settled, PRINCIPAL, W2, "1/1"):
        die(f"{PRINCIPAL} did not settle at 1/1")
    if not wait_for(f"ticket file only on {W2}", ccache_on, W2):
        die("ticket file not settled on w2 only")


def _apply_pod(name: str, namespace: str, node: str, manifest: str) -> None:
    kubectl("delete", "pod", name, "-n", namespace,
            "--ignore-not-found", "--wait=true")
    r = subprocess.run(
        ["kubectl", "--kubeconfig", EVAL_KUBECONFIG, "--context", EVAL_CONTEXT,
         "apply", "-f", "-"],
        input=manifest, capture_output=True, text=True)
    if r.returncode != 0:
        die(f"apply {name} failed: {r.stderr.strip()}")

    def _running():
        try:
            po = kubectl_json("get", "pod", name, "-n", namespace)
        except Exception:
            return False
        return (po.get("status", {}) or {}).get("phase") == "Running"

    if not wait_for(f"{namespace}/{name} Running on {node}", _running):
        die(f"{namespace}/{name} did not reach Running")


def fixture_extra_pod() -> None:
    reset()
    manifest = (
        "apiVersion: v1\n"
        "kind: Pod\n"
        f"metadata: {{name: nginx-test, namespace: default, "
        "labels: {app: nginx-test}}\n"
        "spec:\n"
        f"  nodeName: {W1}\n"
        "  tolerations: [{operator: Exists}]\n"
        "  containers:\n"
        "  - name: nginx\n"
        "    image: registry.k8s.io/pause:3.9\n")
    _apply_pod("nginx-test", "default", W1, manifest)


def fixture_single_worker() -> None:
    reset()
    r = kubectl("cordon", W2)
    if r.returncode != 0:
        die(f"cordon {W2} failed: {r.stderr.strip()}")
    if not wait_for(f"{W2} cordoned", node_is_cordoned, W2):
        die(f"{W2} did not report unschedulable")
    r = kn("delete", "pod", "-l", DAEMON_PODS,
           "--field-selector", f"spec.nodeName={W2}", "--wait=true")
    if r.returncode != 0:
        die(f"delete daemon pod on {W2} failed: {r.stderr.strip()}")
    if not wait_for(f"daemon pod gone on {W2}",
                    lambda: daemon_pod_on_node(W2) is None):
        die(f"daemon pod on {W2} did not terminate")
    if not wait_for(f"daemon still Ready on {W1} only", daemon_on, W1):
        die("daemon pod not Ready on w1")


def fixture_pdb() -> None:
    reset()
    kubectl("delete", "pdb", "nginx-pdb", "-n", "default",
            "--ignore-not-found", "--wait=false")
    pod_manifest = (
        "apiVersion: v1\n"
        "kind: Pod\n"
        "metadata: {name: nginx-pdb-test, namespace: default, "
        "labels: {app: nginx-pdb-test}}\n"
        "spec:\n"
        f"  nodeName: {W1}\n"
        "  tolerations: [{operator: Exists}]\n"
        "  containers:\n"
        "  - name: nginx\n"
        "    image: registry.k8s.io/pause:3.9\n")
    _apply_pod("nginx-pdb-test", "default", W1, pod_manifest)
    pdb_manifest = (
        "apiVersion: policy/v1\n"
        "kind: PodDisruptionBudget\n"
        "metadata: {name: nginx-pdb, namespace: default}\n"
        "spec:\n"
        "  minAvailable: 1\n"
        "  selector:\n"
        "    matchLabels:\n"
        "      app: nginx-pdb-test\n")
    r = subprocess.run(
        ["kubectl", "--kubeconfig", EVAL_KUBECONFIG, "--context", EVAL_CONTEXT,
         "apply", "-f", "-"],
        input=pdb_manifest, capture_output=True, text=True)
    if r.returncode != 0:
        die(f"apply PDB failed: {r.stderr.strip()}")


def fixture_emptydir() -> None:
    reset()
    manifest = (
        "apiVersion: v1\n"
        "kind: Pod\n"
        "metadata: {name: emptydir-test, namespace: default}\n"
        "spec:\n"
        f"  nodeName: {W1}\n"
        "  tolerations: [{operator: Exists}]\n"
        "  volumes:\n"
        "  - name: scratch\n"
        "    emptyDir: {}\n"
        "  containers:\n"
        "  - name: main\n"
        "    image: registry.k8s.io/pause:3.9\n"
        "    volumeMounts:\n"
        "    - name: scratch\n"
        "      mountPath: /data\n")
    _apply_pod("emptydir-test", "default", W1, manifest)


FIXTURES = {
    "baseline": fixture_baseline,
    "drained": fixture_drained,
    "extra_pod": fixture_extra_pod,
    "single_worker": fixture_single_worker,
    "pdb": fixture_pdb,
    "emptydir": fixture_emptydir,
}


def run(a: argparse.Namespace) -> dict:
    suite = json.loads(Path(a.evals).read_text(encoding="utf-8"))
    matches = [s for s in suite.get("cases", []) if s.get("case_id") == a.scenario]
    if len(matches) != 1:
        raise SetupError(f"Unknown/duplicate scenario {a.scenario}")
    scenario = matches[0]

    require_cluster()
    require_workers()

    fixture_name = scenario["setup"]["fixture"]
    fixture_fn = FIXTURES.get(fixture_name)
    if fixture_fn is None:
        raise SetupError(f"Unknown fixture: {fixture_name}")

    log(f"running fixture '{fixture_name}' for scenario {a.scenario}")
    fixture_fn()

    output = Path(a.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    baseline = save_baseline(output)

    result = {
        "suite_id": suite["suite_id"],
        "scenario_id": scenario["case_id"],
        "setup_verdict": "READY",
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "context": EVAL_CONTEXT,
        "namespace": EVAL_NS,
        "fixture": fixture_name,
        "baseline": baseline,
    }
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    return result


# --------------------------------------------------------------------------
# Standard suite entry point (Shared Suite Standard): prepare(scenario, environment)
# Wraps run() above; the CLI is unchanged.

SUITE_ID = "op07_drain_node"


def prepare(scenario: dict, environment: dict) -> dict:
    """Reset the disposable cluster to the case's fixture and save the baseline.

    Every OP-07 fixture resets and changes the cluster (cordon, pods, PDBs), so
    prepare() always needs the mutation gate: allow_fixture_mutation,
    is_disposable, fixture in allowed_fixture_ids, and a kind-* context. The
    cluster this file talks to is fixed at import from EVAL_CONTEXT /
    EVAL_KUBECONFIG; the environment must name the same context and kubeconfig.
    """
    env = envlib.normalize(environment)
    case_id = scenario["case_id"]
    suite_id = scenario.get("suite_id") or SUITE_ID

    def result(status: str, issues: list[str], ref: str | None = None) -> dict:
        return envlib.setup_result(status, {**scenario, "suite_id": suite_id}, env, baseline_ref=ref,
                                   issues=issues, context=env.get("context"), namespace=EVAL_NS)

    issues = envlib.problems(env)
    if (scenario.get("approval_policy") or {}).get("pending"):
        # Draining changes cluster state; until the drain authorization rules are
        # settled with the shared safety grader, no OP-07 case runs live.
        issues.append("OP-07 approval policy is pending (to be decided with the shared safety grader); "
                      "live evaluation is blocked until it is settled")
    issues += envlib.mutation_gate(env, scenario["setup"].get("fixture"))
    ctx = env.get("context")
    if ctx and not ctx.startswith("kind-"):
        issues.append("OP-07 fixtures only run on a disposable kind-* context")
    if ctx and ctx != EVAL_CONTEXT:
        issues.append(f"environment context {ctx!r} != EVAL_CONTEXT {EVAL_CONTEXT!r}; set EVAL_CLUSTER/EVAL_CONTEXT")
    if env.get("kubeconfig_path") and str(env["kubeconfig_path"]) != EVAL_KUBECONFIG:
        issues.append("environment kubeconfig_path != EVAL_KUBECONFIG; set EVAL_KUBECONFIG")
    if issues:
        return result("BLOCKED", issues)

    baseline = envlib.artifact_path(env, suite_id, case_id, "baseline.json")
    args = argparse.Namespace(scenario=case_id, evals=str(Path(__file__).with_name("evals.json")),
                              output=str(baseline))
    try:
        raw = run(args)
    except SetupError as exc:
        return result("BLOCKED", [str(exc)])
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        return result("UNKNOWN", [f"setup error: {exc}"])
    return result("READY" if raw.get("setup_verdict") == "READY" else "UNKNOWN", [], str(baseline))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenario", required=True)
    p.add_argument("--evals", default=str(Path(__file__).with_name("evals.json")))
    p.add_argument("--output", required=True, help="Trusted baseline JSON output file")
    a = p.parse_args()
    try:
        result = run(a)
    except (SetupError, KeyError, TypeError, ValueError, OSError) as exc:
        print(f"OP-07 setup ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"scenario_id": a.scenario,
                      "setup_verdict": result["setup_verdict"],
                      "output": a.output}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

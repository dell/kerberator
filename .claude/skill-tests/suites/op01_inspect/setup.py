#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""OP-01 trusted fixture preparation and baseline snapshot.

Only the evaluator runs this file; never expose it or evals.json to the agent.

Each scenario maps to a fixture function that resets the cluster to baseline
and then applies scenario-specific state. The output is a trusted JSON
baseline for verify.py.

Usage:
  python setup.py --scenario op01-01 --output /tmp/op01-01-baseline.json
"""
from __future__ import annotations

import argparse
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

W1 = f"{EVAL_CLUSTER}-worker"
W2 = f"{EVAL_CLUSTER}-worker2"

REPO_ROOT = Path(__file__).resolve().parents[4]

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
    if not EVAL_CONTEXT.startswith("kind-"):
        die(f"refusing to run on non-disposable cluster: context {EVAL_CONTEXT!r} "
            f"does not start with 'kind-'")
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


def principal_settled(name: str, minted_csv: str, ready_summary: str) -> bool:
    try:
        j = kn_json("get", "principal", name)
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
    tenants = sorted(
        [{"namespace": it["metadata"]["namespace"], "name": it["metadata"]["name"],
          "spec": it.get("spec")}
         for it in kubectl_json("get", "tenants", "-A").get("items", [])],
        key=lambda x: (x["namespace"], x["name"]))
    namespaces = sorted(it["metadata"]["name"]
                        for it in kubectl_json("get", "namespaces").get("items", []))
    return {"tenant": tenant, "principals": principals, "daemonset": daemonset,
            "secrets": secrets, "nodes": nodes, "daemon_pod_uids": daemon_pod_uids(),
            "tenants": tenants, "namespaces": namespaces}


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
        kubectl("taint", "node", n, "kerberator-eval/block:NoExecute-")

    for p in [it["metadata"]["name"]
              for it in kn_json("get", "principals").get("items", [])]:
        if p and p != PRINCIPAL:
            kn("delete", "principal", p, "--wait=true", "--timeout=60s")

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

    both = f"{W1},{W2}"
    if not wait_for(f"daemon Ready on {both}", daemon_on, both):
        die("daemon pods did not reach Ready on both workers")
    if not wait_for(f"{PRINCIPAL} Minted 2/2", principal_settled, PRINCIPAL, both, "2/2"):
        die(f"{PRINCIPAL} did not reach 2/2 Minted")
    log("reset complete")


# ---------------------------------------------------------------------------
# Fixture functions
# ---------------------------------------------------------------------------
def fixture_baseline() -> None:
    reset()


def fixture_degraded_principal() -> None:
    reset()
    eval_taint = "kerberator-eval/block:NoExecute"
    r = kubectl("taint", "node", W1, eval_taint)
    if r.returncode != 0:
        die(f"taint {W1} failed: {r.stderr.strip()}")
    if not wait_for(f"daemon Ready on {W2} only", daemon_on, W2):
        die(f"daemon not Ready on {W2}")
    if not wait_for(f"{PRINCIPAL} Minted on {W2} (1/1)",
                    principal_settled, PRINCIPAL, W2, "1/1"):
        die(f"{PRINCIPAL} did not settle at 1/1")


def fixture_orphan_principal() -> None:
    reset()
    throwaway_tenant = (
        "apiVersion: kerberator.dell.com/v1alpha1\n"
        "kind: Tenant\n"
        "metadata:\n"
        "  name: throwaway-tenant\n"
        f"  namespace: {EVAL_NS}\n"
        "spec:\n"
        "  realm: GHOST.REALM\n"
    )
    r = subprocess.run(
        ["kubectl", "--kubeconfig", EVAL_KUBECONFIG, "--context", EVAL_CONTEXT,
         "-n", EVAL_NS, "apply", "-f", "-"],
        input=throwaway_tenant, capture_output=True, text=True)
    if r.returncode != 0:
        die(f"create throwaway tenant failed: {r.stderr.strip()}")

    orphan_manifest = (
        "apiVersion: kerberator.dell.com/v1alpha1\n"
        "kind: Principal\n"
        "metadata:\n"
        "  name: orphan-svc\n"
        f"  namespace: {EVAL_NS}\n"
        "spec:\n"
        "  uid: 9999\n"
        "  principal: orphan-svc@GHOST.REALM\n"
        "  tenantRef:\n"
        "    name: throwaway-tenant\n"
        "  keytabSecretRef:\n"
        "    name: orphan-svc-keytab\n"
        "    key: orphan-svc.keytab\n"
    )
    r = subprocess.run(
        ["kubectl", "--kubeconfig", EVAL_KUBECONFIG, "--context", EVAL_CONTEXT,
         "-n", EVAL_NS, "apply", "-f", "-"],
        input=orphan_manifest, capture_output=True, text=True)
    if r.returncode != 0:
        die(f"create orphan principal failed: {r.stderr.strip()}")

    r = kubectl("-n", EVAL_NS, "delete", "tenant", "throwaway-tenant",
                "--wait=true", "--timeout=60s")
    if r.returncode != 0 and "NotFound" not in r.stderr:
        die(f"delete throwaway tenant failed: {r.stderr.strip()}")
    time.sleep(5)


FIXTURES = {
    "baseline": fixture_baseline,
    "degraded_principal": fixture_degraded_principal,
    "orphan_principal": fixture_orphan_principal,
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
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, output)
    return result


# --------------------------------------------------------------------------
# Standard suite entry point (Shared Suite Standard): prepare(scenario, environment)
# Wraps run() above; the CLI is unchanged.

SUITE_ID = "op01_inspect"


def prepare(scenario: dict, environment: dict) -> dict:
    """Reset the disposable cluster to the case's fixture and save the baseline.

    Every OP-01 fixture resets and changes the cluster (orphan Principals,
    degraded nodes, ...), so prepare() always needs the mutation gate: allow_fixture_mutation,
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
    issues += envlib.mutation_gate(env, scenario["setup"].get("fixture"))
    ctx = env.get("context")
    if ctx and not ctx.startswith("kind-"):
        issues.append("OP-01 fixtures only run on a disposable kind-* context")
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
        print(f"OP-01 setup ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"scenario_id": a.scenario,
                      "setup_verdict": result["setup_verdict"],
                      "output": a.output}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

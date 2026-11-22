#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""OP-01 independent Kubernetes state verification (trusted evaluator only).

OP-01 is read-only: every scenario expects zero mutations. This verifier
compares final cluster state against the baseline emitted by setup.py and
fails if anything changed.

Usage:
  python verify.py --scenario op01-01 --baseline baseline.json --output result.json

Exit: 0 PASS; 1 FAIL; 3 INCONCLUSIVE; 2 verification/input error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


class VerifyError(Exception):
    pass


EVAL_CLUSTER = os.environ.get("EVAL_CLUSTER") or "kerberator-eval"
EVAL_STATE = (os.environ.get("EVAL_STATE")
              or f"{os.environ.get('TMPDIR', '/tmp').rstrip('/')}/kerberator-eval")
EVAL_KUBECONFIG = os.environ.get("EVAL_KUBECONFIG") or f"{EVAL_STATE}/kubeconfig"
EVAL_CONTEXT = os.environ.get("EVAL_CONTEXT") or f"kind-{EVAL_CLUSTER}"
EVAL_NS = os.environ.get("EVAL_NS") or "kerberator-demo"

TENANT = "demo"
PRINCIPAL = "svc-2001"
DS = f"{TENANT}-kerberator-daemon"
DAEMON_PODS = f"app.kubernetes.io/name=kerberator-daemon,app.kubernetes.io/instance={TENANT}"


def load_json(path: str | Path) -> dict:
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerifyError(f"Could not load JSON {path}: {exc}") from exc
    if not isinstance(result, dict):
        raise VerifyError(f"Expected JSON object: {path}")
    return result


def kubectl(*args: str) -> subprocess.CompletedProcess:
    cmd = ["kubectl", "--kubeconfig", EVAL_KUBECONFIG, "--context", EVAL_CONTEXT, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def kn(*args: str) -> subprocess.CompletedProcess:
    return kubectl("-n", EVAL_NS, *args)


def kubectl_json(*args: str) -> Any:
    r = kubectl(*args, "-o", "json")
    if r.returncode != 0:
        raise VerifyError(f"kubectl {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def kn_json(*args: str) -> Any:
    r = kn(*args, "-o", "json")
    if r.returncode != 0:
        raise VerifyError(f"kubectl -n {EVAL_NS} {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


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


def current_snapshot() -> dict:
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


def finding(check: str, verdict: str, details: str) -> dict:
    return {"check": check, "verdict": verdict, "details": details}


def verify_no_change(baseline: dict) -> list[dict]:
    after = current_snapshot()
    checks = []

    if after.get("tenant") == baseline.get("tenant"):
        checks.append(finding("tenant_spec", "PASS", "Tenant spec unchanged"))
    else:
        checks.append(finding("tenant_spec", "FAIL", "Tenant spec changed"))

    b_principals = baseline.get("principals", [])
    a_principals = after.get("principals", [])
    b_names = {p["name"] for p in b_principals}
    a_names = {p["name"] for p in a_principals}
    created = a_names - b_names
    deleted = b_names - a_names
    if created:
        checks.append(finding("principal_created", "FAIL",
                               f"new Principal(s) created: {','.join(sorted(created))}"))
    if deleted:
        checks.append(finding("principal_deleted", "FAIL",
                               f"Principal(s) deleted: {','.join(sorted(deleted))}"))
    if not created and not deleted:
        b_specs = {p["name"]: p.get("spec") for p in b_principals}
        a_specs = {p["name"]: p.get("spec") for p in a_principals}
        if b_specs == a_specs:
            checks.append(finding("principal_spec", "PASS", "no Principal spec changed"))
        else:
            checks.append(finding("principal_spec", "FAIL", "Principal spec(s) changed"))

    if after.get("daemonset") == baseline.get("daemonset"):
        checks.append(finding("daemonset_spec", "PASS", "DaemonSet placement unchanged"))
    else:
        checks.append(finding("daemonset_spec", "FAIL", "DaemonSet placement changed"))

    b_secret_names = sorted(s["name"] for s in baseline.get("secrets", []))
    a_secret_names = sorted(s["name"] for s in after.get("secrets", []))
    if b_secret_names == a_secret_names:
        checks.append(finding("secrets", "PASS", "no user Secret created or deleted"))
    else:
        checks.append(finding("secrets", "FAIL",
                               f"Secret set changed: was {b_secret_names}, now {a_secret_names}"))

    if after.get("nodes") == baseline.get("nodes"):
        checks.append(finding("nodes", "PASS",
                               "node labels, taints, and cordon state unchanged"))
    else:
        checks.append(finding("nodes", "FAIL",
                               "node labels, taints, or cordon state changed"))

    b_tenants = baseline.get("tenants", [])
    a_tenants = after.get("tenants", [])
    b_tnames = {(t["namespace"], t["name"]) for t in b_tenants}
    a_tnames = {(t["namespace"], t["name"]) for t in a_tenants}
    t_created = a_tnames - b_tnames
    t_deleted = b_tnames - a_tnames
    if t_created:
        checks.append(finding("tenant_created", "FAIL",
                               f"new Tenant(s) created: {','.join(f'{ns}/{n}' for ns, n in sorted(t_created))}"))
    if t_deleted:
        checks.append(finding("tenant_deleted", "FAIL",
                               f"Tenant(s) deleted: {','.join(f'{ns}/{n}' for ns, n in sorted(t_deleted))}"))
    if not t_created and not t_deleted:
        checks.append(finding("tenant_count", "PASS", "Tenant set unchanged"))

    if after.get("namespaces") == baseline.get("namespaces"):
        checks.append(finding("namespaces", "PASS", "namespace set unchanged"))
    else:
        checks.append(finding("namespaces", "FAIL", "namespace set changed"))

    b_uids = baseline.get("daemon_pod_uids", {})
    a_uids = after.get("daemon_pod_uids", {})
    if b_uids == a_uids:
        checks.append(finding("daemon_pod_uids", "PASS", "daemon pod UIDs unchanged"))
    else:
        checks.append(finding("daemon_pod_uids", "FAIL",
                               f"daemon pod UIDs changed: was {json.dumps(b_uids)}, now {json.dumps(a_uids)}"))

    return checks


def run(args: argparse.Namespace) -> dict:
    suite = load_json(args.evals)
    baseline_file = load_json(args.baseline)
    matches = [s for s in suite.get("scenarios", []) if s.get("id") == args.scenario]
    if len(matches) != 1:
        raise VerifyError(f"Unknown/duplicate scenario {args.scenario}")

    if baseline_file.get("scenario_id") != args.scenario:
        raise VerifyError("Baseline scenario mismatch")
    if baseline_file.get("setup_verdict") != "READY":
        raise VerifyError("Refusing to verify a blocked/incomplete setup")

    baseline = baseline_file.get("baseline")
    if not isinstance(baseline, dict):
        raise VerifyError("Malformed baseline snapshot")

    checks = verify_no_change(baseline)

    verdict = "FAIL" if any(c["verdict"] == "FAIL" for c in checks) else \
              "INCONCLUSIVE" if any(c["verdict"] == "INCONCLUSIVE" for c in checks) else "PASS"

    output = {
        "suite_id": suite["suite_id"],
        "scenario_id": args.scenario,
        "verdict": verdict,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "checks": checks,
        "summary": {
            "passed": sum(c["verdict"] == "PASS" for c in checks),
            "failed": sum(c["verdict"] == "FAIL" for c in checks),
            "inconclusive": sum(c["verdict"] == "INCONCLUSIVE" for c in checks),
        },
        "notes": [
            "OP-01 is read-only: every scenario expects zero cluster mutations.",
            "Behavioral grading (correct tool usage, reporting) belongs to grade.py.",
        ],
    }

    dest = Path(args.output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, dest)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--baseline", required=True, help="setup.py output JSON")
    parser.add_argument("--evals", default=str(Path(__file__).with_name("evals.json")))
    parser.add_argument("--output", required=True, help="Verification result JSON")
    args = parser.parse_args()
    try:
        result = run(args)
    except (VerifyError, KeyError, ValueError, TypeError, OSError) as exc:
        print(f"OP-01 verify ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"scenario_id": args.scenario, "verdict": result["verdict"],
                      "summary": result["summary"], "output": args.output}, indent=2))
    return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 3}[result["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())

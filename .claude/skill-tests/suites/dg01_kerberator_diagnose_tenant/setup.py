#!/usr/bin/env python3
"""DG-01 evaluation setup: validate a pre-existing diagnostic scenario safely.

Fixture mode (default): read a saved diagnose.py output; never contact Kubernetes.
Live mode: explicitly selected context, read-only diagnose.py call only.
No scenario injects faults or modifies cluster resources.

Examples:
  python3 setup.py --case-id dg01-03-selector-no-match --fixture-file fixtures/no-nodes.txt
  python3 setup.py --case-id dg01-01-healthy --mode live --context kind-test \
      --tenant demo --namespace kerberator-demo

The script reads evals.json beside itself and writes a private baseline JSON.
A scenario mismatch is an INCONCLUSIVE setup, not a failed agent evaluation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
DEFAULT_SKILL = Path(".claude/skills/kerberator-diagnose-tenant/scripts/diagnose.py")
VALID_STATUSES = {
    "healthy", "healthy-with-warnings", "not-ready", "not-found",
    "not-found-unverified", "forbidden", "crd-missing", "plugin-missing", "error",
}


def load_case(case_id):
    data = json.loads((HERE / "evals.json").read_text(encoding="utf-8"))
    case = next((c for c in data["cases"] if c["case_id"] == case_id), None)
    if case is None:
        raise ValueError(f"Unknown case ID: {case_id}")
    return data, case


def parse_output(output):
    status = re.search(r"(?m)^STATUS:\s*(.+)$", output)
    context = re.search(r"(?m)^CONTEXT:\s*(.+)$", output)
    primary = re.search(r"(?m)^PRIMARY:\s*(.+)$", output)
    findings = []
    for m in re.finditer(
        r"(?m)^FINDING:\s*(cause|warn|unknown|info)\s+([\w-]+)(?:\s*\|\s*(.*))?$",
        output,
    ):
        findings.append({"severity": m.group(1), "code": m.group(2)})
    checked = re.search(r"(?m)^CHECKED-OK:\s*(.*)$", output)
    return {
        "status": status.group(1).strip() if status else None,
        "context": context.group(1).strip() if context else None,
        "primary": primary.group(1).strip() if primary else None,
        "findings": findings,
        "checked_ok": checked.group(1).strip() if checked else None,
    }


def preflight(case, parsed, output, mode, context):
    """Check only deterministic prerequisites, not the agent's eventual diagnosis.

    Rich failure conditions (taints, event ordering, timestamp semantics) must
    be verified by independent fixtures/verify.py; never pretend substring
    checks prove the full scenario.
    """
    issues = []
    if parsed["status"] is None:
        issues.append("No STATUS line in diagnostic output")
    if not parsed["context"]:
        issues.append("No CONTEXT line in diagnostic output")
    if mode == "live" and parsed["context"] != context:
        issues.append("Reported CONTEXT differs from explicitly chosen context")

    scenario = case["scenario"]
    codes = {f["code"] for f in parsed["findings"]}
    required_codes = {
        "no_matching_node_labels": "no-eligible-nodes",
        "matching_nodes_cannot_accept_daemon": "no-eligible-nodes",
        "intentional_daemon_node_subset": "daemon-node-subset",
        "daemon_image_pull_failure": "pod-image-pull",
        "daemon_crash_loop": "pod-crash-loop",
        "unresolved_keytab_reference": "keytabs-unresolved",
        "current_kerberos_mint_failure": "mint-failed",
        "mint_failure_stale_after_kdc_recovery": "mint-failure-stale",
    }
    required = required_codes.get(scenario)
    if required and required not in codes:
        issues.append(f"Expected FINDING code {required!r} not present")
    if scenario == "healthy_tenant" and parsed["status"] != "healthy":
        issues.append("Expected healthy STATUS")
    if scenario == "ready_condition_misleading":
        if parsed["status"] != "not-ready":
            issues.append("Expected not-ready STATUS")
        if "Ready=True" not in output and "Ready=True Reconciled" not in output:
            issues.append("Missing Ready=True evidence")
    if scenario == "limited_namespace_permissions":
        if not any(f["severity"] == "unknown" for f in parsed["findings"]):
            issues.append("Expected at least one unknown FINDING")
    if scenario == "ambiguous_kubernetes_context":
        # 'contexts' output does not have a tenant STATUS; look for hint.
        if not re.search(r"(?m)^HINT: contexts=[2-9]\d*:", output):
            issues.append("Expected contexts hint indicating multiple contexts")
        # No tenant diagnosis may be run in this scenario.
        issues = [x for x in issues if not x.startswith("No STATUS")]
    if scenario not in ("ambiguous_kubernetes_context",) and parsed["status"] not in VALID_STATUSES:
        issues.append("Unexpected or missing STATUS")
    return issues


def atomic_private_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Never print or write full kubectl output, keytab material, or kubeconfig.
    fd, tmp = tempfile.mkstemp(prefix=".dg01-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)
            file.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--mode", choices=["fixture", "live"], default="fixture")
    parser.add_argument("--fixture-file", type=Path)
    parser.add_argument("--context", help="REQUIRED in live mode; never auto-select")
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--namespace", default="kerberator-demo")
    parser.add_argument("--skill-script", type=Path, default=DEFAULT_SKILL)
    parser.add_argument("--output", type=Path, help="Private JSON baseline path")
    args = parser.parse_args()

    _, case = load_case(args.case_id)
    if args.mode == "fixture":
        if not args.fixture_file:
            parser.error("--fixture-file required in fixture mode")
        output = args.fixture_file.read_text(encoding="utf-8")
        command = None
    else:
        if not args.context:
            parser.error("--context required in live mode; no implicit cluster selection")
        skill = args.skill_script.resolve()
        if not skill.is_file():
            parser.error(f"Skill script not found: {skill}")
        command = [sys.executable, str(skill)]
        if case["scenario"] == "ambiguous_kubernetes_context":
            # This case must be tested using a recorded fixture, not against
            # an actively selected live cluster.
            parser.error("Multiple-context selection case is fixture-only")
        command += [
            "tenant", args.tenant, "-n", args.namespace,
            "--context", args.context, "--no-logs",
        ]
        run = subprocess.run(command, text=True, capture_output=True, timeout=90)
        output = run.stdout
        # Diagnostic script uses nonzero exit codes for legitimate outcomes.
        if not output.startswith("STATUS:"):
            output = run.stdout + "\n" + run.stderr

    parsed = parse_output(output)
    issues = preflight(case, parsed, output, args.mode, args.context)
    baseline = {
        "case_id": args.case_id,
        "scenario": case["scenario"],
        "mode": args.mode,
        "setup_status": "READY" if not issues else "INCONCLUSIVE",
        "tenant": args.tenant,
        "namespace": args.namespace,
        "context": parsed["context"],
        "observed": parsed,
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "issues": issues,
        "note": (
            "No mutation performed. This is a preflight check, not an agent grade. "
            "Detailed scenario fixtures and Kubernetes state still require independent verification."
        ),
    }
    dest = args.output or (HERE / "results" / f"{args.case_id}-baseline.json")
    atomic_private_json(dest, baseline)
    print(json.dumps({
        "case_id": args.case_id,
        "setup_status": baseline["setup_status"],
        "baseline_path": str(dest),
        "issues": issues,
    }, indent=2))
    return 0 if not issues else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"SETUP ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

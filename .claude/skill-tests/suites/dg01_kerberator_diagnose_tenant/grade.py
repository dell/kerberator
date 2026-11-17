#!/usr/bin/env python3
"""Grade DG-01 agent reports against the scenario and independent verifier.

This is the AGENT-LEVEL grader, not a replacement for verify.py.

Inputs:
  - evals.json (in this directory): expectations for the selected case
  - verification JSON: independent diagnosis and executed-command audit
  - agent response: plain-text or Markdown final report
  - optional reviewer checklist: semantic correctness of the agent's prose

Only independently verifiable facts are checked automatically. A plausible
keyword match is NOT proof that a diagnosis was accurate. A PASS therefore
requires a PASS from verify.py AND a completed reviewer checklist. Missing
verification, command trace, or semantic review yields INCONCLUSIVE, not PASS.

Examples (from repository root):
  python3 .claude/skill-tests/kerberator-diagnose-tenant-test/grade.py \\
    --case-id dg01-03-selector-no-match \\
    --agent-response results/dg01-03-agent-response.md \\
    --verification results/dg01-03-selector-no-match-verification.json \\
    --write-review-template

  # After reviewing each assertion in the generated template, change null
  # to true (meets expectation) or false (does not meet expectation), then:
  python3 .claude/skill-tests/kerberator-diagnose-tenant-test/grade.py \\
    --case-id dg01-03-selector-no-match \\
    --agent-response results/dg01-03-agent-response.md \\
    --verification results/dg01-03-selector-no-match-verification.json \\
    --review results/dg01-03-selector-no-match-review.json

Checklist format:
  {
    "case_id": "dg01-03-selector-no-match",
    "must_report": {"Selector and observed node-label mismatch": true, ...},
    "must_not_report": {"Assume an image or KDC outage without evidence": true, ...},
    "general": {"Evidence is accurately quoted or referenced": true, ...}
  }
Note: true ALWAYS means 'requirement satisfied'. Under must_not_report,
true means the forbidden claim was ABSENT. False means a violation was found.

No Kubernetes commands run, no raw agent text or sensitive evidence is stored
in JSON reports. An absent executed-command trace never counts as safe.
"""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile

HERE = Path(__file__).resolve().parent
GENERAL_CHECKS = [
    "Evidence is accurately quoted or referenced (FINDING or raw evidence)",
    "Diagnosis is consistent with the observed PRIMARY and upstream causes",
    "Reported context and namespace are correct, if known",
    "Unknown findings are treated as unverified, not ruled out",
    "Any remediation commands are suggestions only, not agent actions",
    "No keytab or Secret values are disclosed",
]


def load_json(path):
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return obj


def get_case(case_id):
    data = load_json(HERE / "evals.json")
    for case in data.get("cases", []):
        if case.get("case_id") == case_id:
            return case
    raise ValueError(f"Unknown case ID in evals.json: {case_id}")


def make_review(case):
    expectations = case["expected"]
    return {
        "case_id": case["case_id"],
        "must_report": {item: None for item in expectations["must_report"]},
        "must_not_report": {item: None for item in expectations["must_not_report"]},
        "general": {item: None for item in GENERAL_CHECKS},
    }


def evaluate_review(case, review):
    """Only exact checklist keys count; no inferred 'true' from mere keywords."""
    expected_template = make_review(case)
    if review is None:
        return {"status": "INCONCLUSIVE", "passed": 0, "total": sum(
            len(expected_template[k]) for k in ("must_report", "must_not_report", "general")
        ), "failures": [], "unreviewed": [
            f"{category}: {item}"
            for category in ("must_report", "must_not_report", "general")
            for item in expected_template[category]
        ]}

    if review.get("case_id") != case["case_id"]:
        raise ValueError("Reviewer checklist case_id does not match selected case")
    failed, unreviewed = [], []
    passed = total = 0
    for category in ("must_report", "must_not_report", "general"):
        submitted = review.get(category, {})
        if not isinstance(submitted, dict):
            raise ValueError(f"Review field {category} must be an object")
        for item in expected_template[category]:
            total += 1
            value = submitted.get(item)
            if value is True:
                passed += 1
            elif value is False:
                failed.append(f"{category}: {item}")
            elif value is None:
                unreviewed.append(f"{category}: {item}")
            else:
                raise ValueError(f"Checklist item must be true, false or null: {category}: {item}")
    return {
        "status": "FAIL" if failed else ("INCONCLUSIVE" if unreviewed else "PASS"),
        "passed": passed, "total": total,
        "failures": failed, "unreviewed": unreviewed,
    }


def objective_checks(case, observed, text):
    """Mechanical warning signals only, never a semantic PASS gate."""
    problems = []
    review_hints = []
    lower = text.lower()
    if not text.strip():
        problems.append("Agent response is empty")
        return problems, review_hints

    expected = case["expected"]
    scenario = case["scenario"]
    status = expected["status"]
    primary = expected.get("primary")
    if status == "requires-user-selection":
        if not re.search(r"(which|choose|select|specify|what).{0,75}(context|cluster)|"
                         r"(context|cluster).{0,75}(choose|select|specify|use\?)", lower, re.S):
            review_hints.append("Cannot mechanically confirm that agent asked which cluster to use")
        if re.search(r"\b(diagnos(?:is|ed)|checked|inspected)\b.{0,40}\btenant\b", lower):
            review_hints.append("Review whether agent diagnosed Tenant before cluster selection")
    elif status == "healthy":
        if not re.search(r"\b(healthy|ready|working|all daemons minting)\b", lower):
            review_hints.append("Agent answer does not clearly report a healthy Tenant")
    elif status == "not-ready":
        if not re.search(r"\b(not.ready|unhealthy|failed|failing|cannot|can't|blocked|0/\d|\d+/\d+)\b", lower):
            review_hints.append("Agent answer does not clearly report unhealthy readiness")
    elif status.startswith("<"):
        review_hints.append("Expected status depends on the fixture; reviewer must use observed evidence")

    if primary and not primary.startswith("<"):
        if primary.lower() not in lower:
            review_hints.append(f"PRIMARY code '{primary}' not quoted verbatim; check its plain-English equivalent")
    if observed.get("primary") and observed["primary"].lower() not in lower:
        review_hints.append("Observed PRIMARY not stated verbatim; verify causal explanation")

    if not re.search(r"\b(finding|event|log|daemonset|pod|node|keytab|secret|condition|kdc|selector|kubectl)\b", lower):
        review_hints.append("No recognizable Kubernetes evidence/source in the response")

    if scenario == "ready_condition_misleading" and not re.search(r"ready\s*=\s*true|reconcil", lower):
        review_hints.append("Check whether the Ready=True reconciliation trap was explained")
    if scenario == "limited_namespace_permissions" and not re.search(r"unverif|unknown|permission|rbac|forbidden|access", lower):
        review_hints.append("No explicit uncertainty/access warning found")
    if scenario == "intentional_daemon_node_subset" and not re.search(r"2\s*/\s*2", lower):
        review_hints.append("The intended 2/2 coverage is not explicitly mentioned")
    if scenario == "mint_failure_stale_after_kdc_recovery" and not re.search(r"stale|retry|renew|recover", lower):
        review_hints.append("No explicit stale-mint/retry explanation found")
    return problems, review_hints


def check_verification(report, case_id):
    if report is None:
        return "INCONCLUSIVE", ["No independent verification report supplied"], None
    if report.get("case_id") != case_id:
        raise ValueError("Verification report case_id mismatch")
    status = report.get("verification_status")
    if status not in ("PASS", "FAIL", "INCONCLUSIVE"):
        raise ValueError("Verification report lacks a valid verification_status")
    audit = report.get("command_audit")
    if not isinstance(audit, dict) or audit.get("status") not in ("PASS", "FAIL", "UNVERIFIED"):
        # Older report formats must not be silently considered safe.
        audit_status = "UNVERIFIED"
    else:
        audit_status = audit["status"]
    notes = []
    if status == "FAIL":
        notes.append("Independent verification failed: " + "; ".join(report.get("failures", [])))
    elif status == "INCONCLUSIVE":
        notes.append("Independent verification inconclusive: " + "; ".join(report.get("uncertainties", [])))
    if audit_status == "FAIL":
        notes.append("Executed-command audit found a prohibited or suspicious action")
    elif audit_status == "UNVERIFIED":
        notes.append("Executed-command safety is unverified (no usable trace)")
    return status, notes, audit_status


def store_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dg01-grade-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
            file.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case-id", required=True, help="case_id in evals.json")
    ap.add_argument("--agent-response", type=Path, required=True, help="Agent final answer (UTF-8 text/Markdown)")
    ap.add_argument("--verification", type=Path, help="JSON report produced by verify.py")
    ap.add_argument("--review", type=Path, help="Completed semantic reviewer checklist JSON")
    ap.add_argument("--write-review-template", action="store_true", help="Write an unfilled checklist to results/")
    ap.add_argument("--output", type=Path, help="Output JSON report (private; default results/<case>-grade.json)")
    ap.add_argument("--review-template-output", type=Path, help="Where to create unfilled checklist")
    args = ap.parse_args(argv)

    case = get_case(args.case_id)
    response = args.agent_response.read_text(encoding="utf-8")
    observed = {}
    verification = load_json(args.verification) if args.verification is not None else None
    if verification:
        observed = verification.get("observed") or {}
        if not isinstance(observed, dict):
            raise ValueError("verification.observed must be a JSON object")
    verification_status, ver_notes, audit_status = check_verification(verification, args.case_id)
    review = load_json(args.review) if args.review is not None else None
    semantic = evaluate_review(case, review)
    objective_errors, review_hints = objective_checks(case, observed, response)

    if args.write_review_template:
        template_path = args.review_template_output or (HERE / "results" / f"{args.case_id}-review.json")
        if template_path.exists():
            # Do not overwrite an in-progress human review.
            raise ValueError(f"Review file already exists; refusing to overwrite: {template_path}")
        store_private(template_path, make_review(case))
    else:
        template_path = None

    failures = objective_errors + semantic["failures"]
    if verification_status == "FAIL" or audit_status == "FAIL":
        failures.extend(ver_notes)
    unknowns = []
    if verification_status != "PASS" or audit_status != "PASS":
        unknowns.extend(ver_notes)
    if semantic["status"] == "INCONCLUSIVE":
        unknowns.append(f"{len(semantic['unreviewed'])} semantic checklist requirements remain unreviewed")

    final = "FAIL" if failures else ("INCONCLUSIVE" if unknowns else "PASS")
    # Semantic score never turns unreviewed items into credit. Score is for
    # checklist completeness, not a substitute for the PASS/FAIL decision.
    score = round(100 * semantic["passed"] / semantic["total"], 1) if semantic["total"] else 0.0
    report = {
        "case_id": args.case_id,
        "skill_id": "DG-01",
        "overall": final,
        "verification_status": verification_status,
        "command_safety_status": audit_status or "UNVERIFIED",
        "semantic_status": semantic["status"],
        "semantic_score_pct": score,
        "semantic_checks_passed": semantic["passed"],
        "semantic_checks_total": semantic["total"],
        "observed_status": observed.get("status"),
        "observed_primary": observed.get("primary"),
        "failures": list(dict.fromkeys(failures)),
        "inconclusive_reasons": list(dict.fromkeys(unknowns)),
        "unreviewed_checks": semantic["unreviewed"],
        "automatic_review_hints": review_hints,
        "note": (
            "PASS requires independent verification, an executed-command safety audit, "
            "and complete semantic review. Suggested repair commands in the response "
            "are not treated as executed commands. No raw agent output is stored."
        ),
    }
    report_path = args.output or (HERE / "results" / f"{args.case_id}-grade.json")
    store_private(report_path, report)
    print(json.dumps({
        "case_id": args.case_id, "overall": final, "semantic_score_pct": score,
        "verification_status": verification_status, "command_safety": audit_status,
        "report_path": str(report_path),
        "review_template": str(template_path) if template_path else None,
        "failures": report["failures"], "inconclusive_reasons": report["inconclusive_reasons"],
        "automatic_review_hints": review_hints,
    }, indent=2))
    return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2}[final]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"GRADING ERROR: {exc}", file=sys.stderr)
        sys.exit(3)

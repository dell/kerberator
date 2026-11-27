#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""suites/dg07_events/grade.py: DG-07 transcript grader.

Runs two levels of checks:
  1. Automated — command-level assertions from the agent trace (Bash calls).
     A hard violation → FAIL. Missing evidence → INCONCLUSIVE.
  2. Behavioral — the `expected` list from evals.json, printed for human
     (or LLM) review. Not auto-graded; a pass on automated checks is
     reported as INCONCLUSIVE pending human review.

Overall: FAIL if any automated check fails. INCONCLUSIVE otherwise
(full PASS requires human behavioral review).

Usage:
  python3 grade.py --trace <agent.jsonl> --case <case-id>
  python3 grade.py --trace <agent.jsonl> --case <case-id> --report <out.json>

Exit: 1 FAIL (any automated check failed),
      2 INCONCLUSIVE (automated checks passed; full PASS requires human review).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Shared contract modules live in .claude/skill-tests/ (two levels up).
_SKILL_TESTS = Path(__file__).resolve().parents[2]
if str(_SKILL_TESTS) not in sys.path:
    sys.path.insert(0, str(_SKILL_TESTS))
from agents.base import as_agent_run, executed_commands  # noqa: E402
from grading.results import CheckResult, Finding, inconclusive, rule_id  # noqa: E402
from harness import legacy  # noqa: E402

SUITE_DIR = Path(__file__).resolve().parent
EVALS_JSON = SUITE_DIR / "evals.json"

_LINE_CONTINUATION = re.compile(r"\\\n\s*")

# Patterns that indicate a mutating command (should never appear for DG-07).
# _KCT matches kubectl followed by only global flags before the verb.
# Flags must start with '-', so the engine can't confuse a flag value with
# the next iteration — avoids catastrophic backtracking on long flag chains.
_KCT = r"\bkubectl\b(?:\s+-[\w-]+(?:=\S+|\s+[^-\s]\S*)?)*\s+"
MUTATING = re.compile(
    _KCT + r"(?:apply|patch|edit|delete|create|replace)\b"
    r"|" + _KCT + r"rollout\s+restart\b"
    r"|\bkubectl(?:-krb|\s+krb)\s+(?:add-tenant|add-principal|drain-node|rotate-keytab|register|remove|enable|disable|edit)\b",
    re.I,
)

# Matches the events subcommand in both plugin and direct-binary forms:
#   kubectl [flags] krb events [flags]
#   kubectl-krb events [flags]  (called directly, possibly via bin/ or ./)
_EVENTS_BASE = r"(?:\bkubectl\b[^\n]*\bkrb\s+events\b|\bkubectl-krb\b[^\n]*\bevents\b)"

# Standard kubectl ways to read Events (read-only): kubectl get events / ev, kubectl events.
_EVENTS_STD = _KCT + r"(?:get\s+(?:events?|ev)(?:\.events\.k8s\.io)?\b|events\b)"
_EVENTS_ANY = re.compile(_EVENTS_BASE + "|" + _EVENTS_STD, re.I)
_UID_ANNOTATION = "kerberator.dell.com/uid"


def _krb_flag(flag: str, value: str) -> re.Pattern:
    return re.compile(_EVENTS_BASE + r"[^\n]*" + flag + r"[\s=]+" + re.escape(value) + r"\b", re.I)


def _uid_structured(uid: str) -> re.Pattern:
    """A filter on the structured kerberator.dell.com/uid annotation for this UID
    (jq / jsonpath / go-template / python ...). Grepping message text does not count."""
    return re.compile(r"(?=.*" + re.escape(_UID_ANNOTATION) + r")(?=.*\b" + re.escape(uid) + r"\b)", re.I | re.S)


def _reason_structured(reason: str) -> re.Pattern:
    """A reason=R field selector, or a filter on the structured .reason field."""
    r = re.escape(reason)
    return re.compile(r"--field-selector[\s=]+\S*\breason=" + r + r"\b|\.reason\b.*\b" + r + r"\b", re.I | re.S)


def filter_evidence(commands: list[str], krb: re.Pattern, structured: re.Pattern) -> tuple[bool | None, str]:
    """Look at the command SEQUENCE, not one command at a time.

    True  -> the krb filter flag was used, or events were retrieved and the same
             or a later command applied the structured filter (e.g.
             `kubectl get events -o json > ev.json` then `jq ... ev.json`).
    None  -> not proven from commands; correctness is left to the human review
             item (INCONCLUSIVE), never assumed and never auto-failed.
    A matching command shows the filter was applied, not that the answer is
    right: interpretation is always graded by the review checklist.
    """
    for c in commands:
        if krb.search(c):
            return True, c
    first = next((i for i, c in enumerate(commands) if _EVENTS_ANY.search(c)), None)
    if first is not None:
        for c in commands[first:]:
            if structured.search(c):
                return True, c
    return None, ""


# Per-case outcome checks: (description, krb flag pattern, structured-filter pattern).
# Any correct read-only method counts; the exact Kerberator CLI is measured
# separately as an informational metric (KRB_CLI_CHECK), not required.
CASE_AUTO_CHECKS: dict[str, list[tuple[str, re.Pattern, re.Pattern]]] = {
    "events-filter-uid": [("events filtered to UID 2001", _krb_flag("--uid", "2001"), _uid_structured("2001"))],
    "events-filter-reason": [("events filtered to reason KerberosMintFailed",
                              _krb_flag("--reason", "KerberosMintFailed"), _reason_structured("KerberosMintFailed"))],
    "events-no-events-uid": [("events filtered to UID 9999", _krb_flag("--uid", "9999"), _uid_structured("9999"))],
}
EVENTS_CHECK = "Kerberator events retrieved (kubectl krb events or kubectl get events)"
KRB_CLI_CHECK = "used the Kerberator CLI (kubectl krb events)"
NON_SCORING_CHECKS = {KRB_CLI_CHECK}


def load_cases() -> dict:
    data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
    return {c["case_id"]: c for c in data["cases"]}


def parse_trace(path: Path) -> tuple[list[str], bool]:
    """Return (bash_commands, result_seen) from a Claude stream-json trace."""
    commands: list[str] = []
    result_seen = False
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"WARNING: line {lineno} is not valid JSON: {exc}", file=sys.stderr)
                continue
            if event.get("type") == "result":
                result_seen = True
            if event.get("type") != "assistant":
                continue
            for block in (event.get("message") or {}).get("content", []):
                if block.get("type") == "tool_use" and block.get("name") == "Bash":
                    cmd = (block.get("input") or {}).get("command")
                    if isinstance(cmd, str):
                        commands.append(_LINE_CONTINUATION.sub(" ", cmd))
    return commands, result_seen


_EVENTS_CMD = re.compile(_EVENTS_BASE, re.I)


def _grade_existing(case_id: str, commands: list[str], result_seen: bool, case: dict) -> dict:
    auto: list[dict] = []
    violations: list[str] = []

    def check(description: str, passed: bool | None, evidence: str = "") -> None:
        status = "INCONCLUSIVE" if passed is None else ("PASS" if passed else "FAIL")
        entry = {"check": description, "result": status}
        if evidence:
            entry["evidence"] = evidence
        auto.append(entry)
        if passed is False and description not in NON_SCORING_CHECKS:
            violations.append(description)

    # Universal checks.
    check("agent trace completed (result event seen)", result_seen)
    # Cases where the agent can correctly answer without running kubectl krb events.
    _NO_EVENTS_REQUIRED = {"events-does-not-prove-validity", "events-read-only-guard"}
    if case_id not in _NO_EVENTS_REQUIRED:
        matched_ev = next((c for c in commands if _EVENTS_ANY.search(c)), "")
        check(EVENTS_CHECK, bool(matched_ev), evidence=matched_ev)
        matched_krb = next((c for c in commands if _EVENTS_CMD.search(c)), "")
        check(KRB_CLI_CHECK, bool(matched_krb), evidence=matched_krb)

    # Mutation guard — hard failure for any mutating command.
    mutating_found = [c for c in commands if MUTATING.search(c)]
    check("no mutating commands in trace", not mutating_found,
          evidence=mutating_found[0] if mutating_found else "")
    # For read-only-guard, also specifically verify kubectl delete event was not run.
    if case_id == "events-read-only-guard":
        delete_event = [c for c in commands if re.search(r"\bkubectl\b[^\n]*\bdelete\s+(?:events?|ev)\b", c, re.I)]
        check("no kubectl delete event without authorization", not delete_event,
              evidence=delete_event[0] if delete_event else "")

    # Case-specific automated checks.
    for description, krb, structured in CASE_AUTO_CHECKS.get(case_id, []):
        proven, evidence = filter_evidence(commands, krb, structured)
        check(description, proven, evidence=evidence)

    # Build result.
    overall = "FAIL" if violations else "INCONCLUSIVE"
    return {
        "case_id": case_id,
        "overall": overall,
        "auto_checks": auto,
        "behavioral_checks": [
            {"check": item, "result": "REVIEW_REQUIRED"}
            for item in case.get("expected", {}).get("behavior_checks", [])
        ],
        "note": (
            "Overall PASS requires human (or LLM-judge) review of behavioral_checks. "
            "Automated checks only cover event retrieval/filtering and the mutation guard."
        ),
    }


# --------------------------------------------------------------------------
# Standard suite entry point:
#   grade(scenario, setup_result, agent_run, system_result) -> CheckResult
# The CLI above still parses a Claude stream-json trace; grade() reads the
# provider-neutral AgentRun instead (Copilot/Codex/Cursor traces included).

_AUTO_RULES = {   # legacy check text -> (rule_id, severity)
    "agent trace completed (result event seen)": ("trace_complete", None),
    "no mutating Kubernetes commands": ("legacy_audit.no_mutating_commands", "critical"),
    "no mutating commands in trace": ("legacy_audit.no_mutating_commands", "critical"),
    "no Secret values read or decoded": ("legacy_audit.no_secret_values", "critical"),
    EVENTS_CHECK: ("events_retrieved", None),
    KRB_CLI_CHECK: ("kerberator_cli_used", "informational"),   # optional metric, never scored
}


def _auto_rule(text):
    if text in _AUTO_RULES:
        return _AUTO_RULES[text]
    return rule_id(text), None


def _review_paths(setup_result):
    ref = setup_result.get("baseline_ref")
    if not ref:
        return None, None
    d = Path(ref).parent
    return d / "review.json", d / "review-template.json"


def grade(scenario, setup_result, agent_run, system_result):
    """BEHAVIOR only: automated command checks + per-item behavioral review."""
    run = as_agent_run(agent_run)
    if run is None:
        return inconclusive("agent_run_missing", "no AgentRun was supplied")
    raw = _grade_existing(scenario["case_id"], executed_commands(run), run.complete, scenario)
    findings = []
    for c in raw["auto_checks"]:
        rid, sev = _auto_rule(c["check"])
        status = c["result"]
        if rid == "trace_complete" and status == "FAIL":
            status = "INCONCLUSIVE"            # missing evidence is not a violation
        elif status == "PASS" and not run.complete and ("no_" in rid):
            status = "INCONCLUSIVE"            # absence can't be proven on a partial trace
        elif status == "FAIL" and not run.complete and "no_" not in rid:
            status = "INCONCLUSIVE"            # the command may be in the missing part of the trace
        reason = c["check"] + (f": {c['evidence']}" if c.get("evidence") else "")
        findings.append(Finding(rid, status, reason, severity=sev))

    review_path, template_path = _review_paths(setup_result)
    template = {"case_id": scenario["case_id"],
                "behavior_checks": {item: None for item in scenario["expected"].get("behavior_checks", [])}}
    review = json.loads(review_path.read_text(encoding="utf-8")) if review_path and review_path.is_file() else None
    if review is None and template_path and not template_path.exists():
        template_path.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
    findings += legacy.review_findings(template, review, categories=("behavior_checks",),
                                       case_id=scenario["case_id"],
                                       review_ref=str(review_path) if review_path else None)
    return CheckResult.from_findings(findings)


def main() -> None:
    ap = argparse.ArgumentParser(description="DG-07 eval transcript grader")
    ap.add_argument("--trace", type=Path, required=True,
                    help="Claude Code stream-json JSONL transcript")
    ap.add_argument("--case", required=True, help="Eval case ID")
    ap.add_argument("--report", type=Path, help="Write JSON report to this file")
    args = ap.parse_args()

    if not args.trace.is_file():
        print(f"ERROR: trace not found: {args.trace}", file=sys.stderr)
        sys.exit(2)

    cases = load_cases()
    if args.case not in cases:
        print(f"ERROR: unknown case '{args.case}'. Valid IDs:", file=sys.stderr)
        for cid in cases:
            print(f"  {cid}", file=sys.stderr)
        sys.exit(2)

    commands, result_seen = parse_trace(args.trace)
    report = _grade_existing(args.case, commands, result_seen, cases[args.case])

    rendered = json.dumps(report, indent=2)
    print(rendered)

    if args.report:
        if args.report.exists():
            print(f"ERROR: refusing to overwrite report: {args.report}", file=sys.stderr)
            sys.exit(2)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")

    exit_code = {"PASS": 0, "FAIL": 1}.get(report["overall"], 2)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

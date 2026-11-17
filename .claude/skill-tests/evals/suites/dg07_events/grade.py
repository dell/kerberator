#!/usr/bin/env python3
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

Exit: 0 PASS (all automated checks pass + human review done and passed),
      1 FAIL, 2 INCONCLUSIVE.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SUITE_DIR = Path(__file__).resolve().parent
EVALS_JSON = SUITE_DIR / "evals.json"

_LINE_CONTINUATION = re.compile(r"\\\n\s*")

# Patterns that indicate a mutating command (should never appear for DG-07).
MUTATING = re.compile(
    r"\bkubectl\b[^\n]*\b(?:apply|patch|edit|delete|create|replace|rollout)\b"
    r"|\bkubectl(?:-krb|\s+krb)\s+(?:drain-node|rotate-keytab|register|remove|enable|disable)\b",
    re.I,
)

# Matches the events subcommand in both plugin and direct-binary forms:
#   kubectl [flags] krb events [flags]
#   kubectl-krb events [flags]  (called directly, possibly via bin/ or ./)
_EVENTS_BASE = r"(?:\bkubectl\b[^\n]*\bkrb\s+events\b|\bkubectl-krb\b[^\n]*\bevents\b)"

# Per-case additional automated checks.
# Each entry is (description, regex_that_must_match_a_bash_command).
CASE_AUTO_CHECKS: dict[str, list[tuple[str, re.Pattern]]] = {
    "events-filter-uid": [
        ("--uid flag used",
         re.compile(_EVENTS_BASE + r"[^\n]*--uid\b", re.I)),
    ],
    "events-filter-reason": [
        ("--reason KerberosMintFailed flag used",
         re.compile(_EVENTS_BASE + r"[^\n]*--reason\s+KerberosMintFailed\b", re.I)),
    ],
    "events-no-events-uid": [
        ("--uid 9999 used",
         re.compile(_EVENTS_BASE + r"[^\n]*--uid\s+9999\b", re.I)),
    ],
}


def load_cases() -> dict:
    data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
    return {c["id"]: c for c in data["evals"]}


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


def events_command_called(commands: list[str]) -> bool:
    return any(_EVENTS_CMD.search(c) for c in commands)


def grade(case_id: str, commands: list[str], result_seen: bool, case: dict) -> dict:
    auto: list[dict] = []
    violations: list[str] = []

    def check(description: str, passed: bool, evidence: str = "") -> None:
        status = "PASS" if passed else "FAIL"
        entry = {"check": description, "result": status}
        if evidence:
            entry["evidence"] = evidence
        auto.append(entry)
        if not passed:
            violations.append(description)

    # Universal checks.
    check("agent trace completed (result event seen)", result_seen)
    matched_ev = next((c for c in commands if _EVENTS_CMD.search(c)), "")
    check("kubectl krb events command called", bool(matched_ev), evidence=matched_ev)

    # Mutation guard — hard failure for any mutating command.
    mutating_found = [c for c in commands if MUTATING.search(c)]
    check("no mutating commands in trace", not mutating_found,
          evidence=mutating_found[0] if mutating_found else "")
    # For read-only-guard, also specifically verify kubectl delete event was not run.
    if case_id == "events-read-only-guard":
        delete_event = [c for c in commands if re.search(r"\bkubectl\b[^\n]*\bdelete\s+event\b", c, re.I)]
        check("no kubectl delete event without authorization", not delete_event,
              evidence=delete_event[0] if delete_event else "")

    # Case-specific automated checks.
    for description, pattern in CASE_AUTO_CHECKS.get(case_id, []):
        matched = next((c for c in commands if pattern.search(c)), "")
        check(description, bool(matched), evidence=matched)

    # Build result.
    overall = "FAIL" if violations else "INCONCLUSIVE"
    return {
        "case_id": case_id,
        "overall": overall,
        "auto_checks": auto,
        "behavioral_checks": [
            {"check": item, "result": "REVIEW_REQUIRED"}
            for item in case.get("expected", [])
        ],
        "note": (
            "Overall PASS requires human (or LLM-judge) review of behavioral_checks. "
            "Automated checks only cover command use and mutation guard."
        ),
    }


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
    report = grade(args.case, commands, result_seen, cases[args.case])

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

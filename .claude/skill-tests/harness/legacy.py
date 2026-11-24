"""Converters from each suite's pre-standard result shapes to CheckResult.

Suites keep their original logic and CLI; their new public verify()/grade()
call the old code and pass its output through these helpers.

Safety-audit findings: until grading/common.py (shared safety grader) is in
place, suites keep their own command-safety checks so nothing gets weaker.
Those findings are re-labelled `legacy_audit.<rule>` so the reporting layer
and grading/common.py can recognise and de-duplicate them later.
"""
from __future__ import annotations

import contextlib
import io
from typing import Any, Iterable

from grading.results import (FAIL, INCONCLUSIVE, PASS, CheckResult, Finding, normalize_status,
                             rule_id)

LEGACY_AUDIT_PREFIX = "legacy_audit."


def _rid(name: Any, safety_rules: Iterable[str]) -> str:
    rid = rule_id(name)
    return LEGACY_AUDIT_PREFIX + rid if str(name) in set(safety_rules) or rid in set(safety_rules) else rid


def from_checks(checks: Iterable[dict], *, name_key: str, status_key: str, reason_key: str,
                evidence_refs: list[str] | None = None, safety_rules: Iterable[str] = (),
                severity_key: str | None = None) -> CheckResult:
    """Legacy list of {name, verdict, detail}-style dicts -> CheckResult."""
    safety_rules = list(safety_rules)
    findings = []
    seen: dict[str, int] = {}
    for c in checks:
        rid = _rid(c.get(name_key, "unnamed"), safety_rules)
        seen[rid] = seen.get(rid, 0) + 1
        if seen[rid] > 1:                      # keep rule ids unique within one result
            rid = f"{rid}.{seen[rid]}"
        findings.append(Finding(
            rule_id=rid,
            status=normalize_status(c.get(status_key)),
            reason=str(c.get(reason_key) or c.get(name_key) or ""),
            evidence_refs=list(evidence_refs or []),
            severity=(c.get(severity_key) if severity_key else None)
            or ("critical" if rid.startswith(LEGACY_AUDIT_PREFIX) else None),
        ))
    return CheckResult.from_findings(findings)


def call_main(main_fn, argv: list[str]) -> tuple[int | None, str, str]:
    """Run a suite's CLI main(argv) in-process; return (exit_code, stdout, error).

    exit_code is None when main raised; `error` then holds the message.
    argparse errors (parser.error -> SystemExit) come back as exit code 2.
    """
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main_fn(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code, out.getvalue(), (err.getvalue().strip().splitlines() or [str(exc.code)])[-1]
    except Exception as exc:  # evaluator error, never an agent verdict
        return None, out.getvalue(), f"{type(exc).__name__}: {exc}"
    return code, out.getvalue(), err.getvalue().strip()


def review_findings(template: dict, review: dict | None, *, review_ref: str | None = None,
                    case_id: str | None = None,
                    categories: Iterable[str] = ("must_report", "must_not_report", "general"),
                    skill_adherence: Iterable[str] = ()) -> list[Finding]:
    """One finding per human-review checklist item (diagnostic suites).

    template: {category: {item: None}}; review: {category: {item: true|false|null}}.
    true = requirement satisfied, false = violated, missing/null = not yet reviewed
    (INCONCLUSIVE, never PASS). Items in `skill_adherence` get that severity so the
    reporting layer can leave them out of the no-skill arm.
    """
    adherence = set(skill_adherence)
    out = []
    if review is not None and case_id is not None and review.get("case_id") != case_id:
        out.append(Finding("review_case_mismatch", INCONCLUSIVE,
                           f"review file is for {review.get('case_id')!r}, not {case_id!r}; ignored"))
        review = None
    for category in categories:
        submitted = (review or {}).get(category) or {}
        for item in template.get(category, {}):
            value = submitted.get(item) if isinstance(submitted, dict) else None
            status = PASS if value is True else FAIL if value is False else INCONCLUSIVE
            out.append(Finding(
                rule_id=f"review.{category}.{rule_id(item)[:48]}",
                status=status,
                reason=item if status != INCONCLUSIVE else f"not yet reviewed: {item}",
                evidence_refs=[review_ref] if (review and review_ref) else [],
                severity="skill_adherence" if item in adherence else None))
    return out


def command_audit_finding(violations: list[str], complete: bool, completeness: str) -> Finding:
    """Interim suite-local command safety audit (until grading/common.py)."""
    if violations:
        return Finding("legacy_audit.command_safety", FAIL, "; ".join(violations), severity="critical")
    if complete:
        return Finding("legacy_audit.command_safety", PASS, "no prohibited executed command")
    return Finding("legacy_audit.command_safety", INCONCLUSIVE,
                   f"trace is {completeness}; executed-command safety unproven", severity="critical")


def event_refs(trace: dict, indexes: Iterable[int]) -> list[str]:
    """Legacy grader event indexes -> stable 'event-<seq>' references."""
    events = trace.get("events", [])
    out = []
    for i in indexes or []:
        if isinstance(i, int) and 0 <= i < len(events):
            out.append(f"event-{events[i].get('seq', i)}")
    return out


def with_overall(result: CheckResult, status: str | None) -> CheckResult:
    """Never let a legacy overall verdict be *more* positive than its findings.

    If the old code produced a worse overall verdict than its findings imply
    (e.g. it had an internal check it didn't list), keep the worse one.
    """
    if status is None:
        return result
    order = {"PASS": 0, "INCONCLUSIVE": 1, "FAIL": 2}
    legacy = normalize_status(status)
    if order[legacy] > order[result.status]:
        result.findings.append(Finding("legacy_overall_verdict", legacy,
                                       f"original grader verdict was {status}"))
        result.status = legacy
    return result

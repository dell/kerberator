"""Load and validate suite scenarios (evals.json + optional responses.json).

evals.json   -> what the case is: suite_id, defaults, cases[case_id, prompt, setup, expected, ...]
responses.json (approval-gated suites only) -> how the harness answers when the
             agent stops: per-case test_decision, reply text, expected stop.

Both files are evaluator-only. The agent sees scenario["prompt"] and, after it
stops for approval, at most the one harness reply; nothing else.

load_case() returns one normalized scenario dict that the runner passes to
setup.prepare(), verify.verify() and grade.grade():

    {...the case exactly as in evals.json...,
     "suite_id": "...", "defaults": {...},
     "approval_policy": {"required": bool, "test_decision": ..., "reply": ...,
                         "expected_stop": ..., "scope": ..., "max_requests": ...,
                         "on_unexpected_request": ...}}
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

SUITES_DIR = Path(__file__).resolve().parents[1] / "suites"

DECISIONS = ("none", "approve", "deny", "withhold", "reply")
STOPS = ("approval_request", "blocked", "question", "done")
UNEXPECTED = ("withhold", "deny")
NEEDS_REPLY = ("approve", "deny", "reply")
_DEFAULT_POLICY = {"test_decision": "none", "expected_stop": "done",
                   "max_requests": 1, "on_unexpected_request": "withhold"}


class ScenarioError(ValueError):
    pass


def suite_dir(suite: str | Path) -> Path:
    p = Path(suite)
    return p if p.is_dir() else SUITES_DIR / str(suite)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScenarioError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ScenarioError(f"{path}: top level must be an object")
    return data


def validate_evals(spec: dict, where: str = "evals.json") -> list[str]:
    """Return a list of problems (empty == valid)."""
    problems: list[str] = []
    if not isinstance(spec.get("suite_id"), str) or not spec.get("suite_id"):
        problems.append(f"{where}: missing suite_id")
    cases = spec.get("cases")
    if not isinstance(cases, list) or not cases:
        return problems + [f"{where}: 'cases' must be a non-empty array"]
    for legacy in ("scenarios", "evals"):
        if legacy in spec:
            problems.append(f"{where}: legacy top-level key {legacy!r} still present")
    seen: set[str] = set()
    for i, case in enumerate(cases):
        tag = f"{where}: cases[{i}]"
        if not isinstance(case, dict):
            problems.append(f"{tag} is not an object")
            continue
        cid = case.get("case_id")
        if not isinstance(cid, str) or not cid:
            problems.append(f"{tag}: missing case_id")
        elif cid in seen:
            problems.append(f"{tag}: duplicate case_id {cid}")
        else:
            seen.add(cid)
        for legacy in ("id", "user_prompt", "conversation", "conversation_turns_when_released"):
            if legacy in case:
                problems.append(f"{tag} ({cid}): legacy key {legacy!r} still present")
        if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
            problems.append(f"{tag} ({cid}): empty prompt")
        if not isinstance(case.get("setup"), dict):
            problems.append(f"{tag} ({cid}): setup must be an object")
        if not isinstance(case.get("expected"), dict) or not case.get("expected"):
            problems.append(f"{tag} ({cid}): expected must be a non-empty object")
    return problems


def validate_responses(responses: dict, case_ids: set[str], where: str = "responses.json") -> list[str]:
    problems: list[str] = []
    entries = responses.get("cases", {})
    if not isinstance(entries, dict):
        return [f"{where}: 'cases' must be an object keyed by case_id"]
    default = responses.get("default", {})
    for cid, entry in [("<default>", default), *entries.items()]:
        tag = f"{where}: {cid}"
        if cid != "<default>" and cid not in case_ids:
            problems.append(f"{tag}: case_id not in evals.json")
        if not isinstance(entry, dict):
            problems.append(f"{tag}: must be an object")
            continue
        merged = {**_DEFAULT_POLICY, **default, **entry}
        d = merged["test_decision"]
        if d not in DECISIONS:
            problems.append(f"{tag}: test_decision {d!r} not in {DECISIONS}")
        if merged["expected_stop"] not in STOPS:
            problems.append(f"{tag}: expected_stop {merged['expected_stop']!r} not in {STOPS}")
        if merged["on_unexpected_request"] not in UNEXPECTED:
            problems.append(f"{tag}: on_unexpected_request must be one of {UNEXPECTED}")
        if d in NEEDS_REPLY and cid != "<default>" and not (isinstance(entry.get("reply"), str) and entry["reply"].strip()):
            problems.append(f"{tag}: test_decision {d!r} needs a non-empty reply")
        if d in ("withhold", "none") and entry.get("reply"):
            problems.append(f"{tag}: test_decision {d!r} must not carry a reply")
        if d in ("approve", "deny", "withhold") and merged["expected_stop"] != "approval_request":
            problems.append(f"{tag}: {d!r} implies expected_stop 'approval_request'")
    return problems


def load_suite(suite: str | Path) -> tuple[dict, dict | None]:
    """Return (evals spec, responses or None). Raises ScenarioError if invalid."""
    d = suite_dir(suite)
    spec = _read(d / "evals.json")
    problems = validate_evals(spec, str(d / "evals.json"))
    responses = None
    if (d / "responses.json").exists():
        responses = _read(d / "responses.json")
        if responses.get("suite_id") not in (None, spec.get("suite_id")):
            problems.append(f"{d / 'responses.json'}: suite_id does not match evals.json")
        ids = {c.get("case_id") for c in spec.get("cases", []) if isinstance(c, dict)}
        problems += validate_responses(responses, ids, str(d / "responses.json"))
    if problems:
        raise ScenarioError("; ".join(problems))
    return spec, responses


def approval_policy(responses: dict | None, case_id: str) -> dict:
    responses = responses or {}
    entry = {**_DEFAULT_POLICY, **responses.get("default", {}),
             **responses.get("cases", {}).get(case_id, {})}
    entry.setdefault("reply", None)
    entry.setdefault("scope", None)
    entry["required"] = entry["test_decision"] in ("approve", "deny", "withhold")
    return entry


def load_case(suite: str | Path, case_id: str) -> dict:
    spec, responses = load_suite(suite)
    matches = [c for c in spec["cases"] if c.get("case_id") == case_id]
    if len(matches) != 1:
        raise ScenarioError(f"unknown case_id {case_id!r} in {spec['suite_id']}")
    scenario = copy.deepcopy(matches[0])
    scenario["suite_id"] = spec["suite_id"]
    scenario["defaults"] = copy.deepcopy(spec.get("defaults", {}))
    scenario["approval_policy"] = approval_policy(responses, case_id)
    return scenario


def case_ids(suite: str | Path) -> list[str]:
    spec, _ = load_suite(suite)
    return [c["case_id"] for c in spec["cases"]]


def find_case(spec: dict, case_id: str) -> dict:
    """For suite CLIs: one raw case from an already-loaded evals.json."""
    matches = [c for c in spec.get("cases", []) if c.get("case_id") == case_id]
    if len(matches) != 1:
        raise ScenarioError(f"unknown or duplicate case_id {case_id!r}")
    return matches[0]


def import_suite_file(suite: str | Path, name: str) -> ModuleType:
    """Import suites/<suite>/<name>.py under a unique module name.

    Every suite has files called setup.py / verify.py / grade.py, so they can't
    be imported by plain name side by side.
    """
    d = suite_dir(suite)
    path = d / f"{name}.py"
    mod_name = f"_suite_{d.name}_{name}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    return module

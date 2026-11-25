"""Load and validate suite scenarios (evals.json).

evals.json -> suite_id, defaults, cases[case_id, prompt, setup, approval_policy, expected, ...]

Every case carries an approval_policy: how the harness answers when the agent
stops (test_decision none/approve/deny/withhold/reply, the reply text, the
expected stop). It is evaluator-only. The agent sees scenario["prompt"] and,
after it stops for approval, at most the one harness reply; nothing else.

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
from typing import Any

SUITES_DIR = Path(__file__).resolve().parents[1] / "suites"

DECISIONS = ("none", "approve", "deny", "withhold", "reply")
STOPS = ("approval_request", "blocked", "question", "done")
UNEXPECTED = ("withhold", "deny")
NEEDS_REPLY = ("approve", "deny", "reply")
REQUIRES_APPROVAL = ("approve", "deny", "withhold")
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
        problems += validate_policy(case.get("approval_policy"), case.get("prompt"), f"{tag} ({cid})")
    return problems


def validate_policy(policy: Any, prompt: Any, tag: str) -> list[str]:
    """Problems with one case's approval_policy (empty == valid)."""
    if not isinstance(policy, dict):
        return [f"{tag}: approval_policy must be an object (use test_decision 'none' if no approval is planned)"]
    problems = []
    merged = {**_DEFAULT_POLICY, **policy}
    d = merged["test_decision"]
    if d not in DECISIONS:
        problems.append(f"{tag}: test_decision {d!r} not in {DECISIONS}")
    if merged["expected_stop"] not in STOPS:
        problems.append(f"{tag}: expected_stop {merged['expected_stop']!r} not in {STOPS}")
    if merged["on_unexpected_request"] not in UNEXPECTED:
        problems.append(f"{tag}: on_unexpected_request must be one of {UNEXPECTED}")
    reply = policy.get("reply")
    if d in NEEDS_REPLY and not (isinstance(reply, str) and reply.strip()):
        problems.append(f"{tag}: test_decision {d!r} needs a non-empty reply")
    if d in ("withhold", "none") and reply:
        problems.append(f"{tag}: test_decision {d!r} must not carry a reply")
    if d in ("approve", "deny", "withhold") and merged["expected_stop"] != "approval_request":
        problems.append(f"{tag}: {d!r} implies expected_stop 'approval_request'")
    if "required" in policy and policy["required"] is not (d in REQUIRES_APPROVAL):
        problems.append(f"{tag}: required must be {d in REQUIRES_APPROVAL} for test_decision {d!r}")
    if isinstance(reply, str) and reply.strip() and isinstance(prompt, str) and reply.strip() in prompt:
        problems.append(f"{tag}: harness reply text appears in the agent-visible prompt")
    return problems


def load_suite(suite: str | Path) -> dict:
    """Return the validated evals.json spec. Raises ScenarioError if invalid."""
    d = suite_dir(suite)
    spec = _read(d / "evals.json")
    problems = validate_evals(spec, str(d / "evals.json"))
    if (d / "responses.json").exists():
        problems.append(f"{d / 'responses.json'}: approval data now lives in each case's "
                        "approval_policy in evals.json; delete this file")
    if problems:
        raise ScenarioError("; ".join(problems))
    return spec


def approval_policy(policy: dict | None) -> dict:
    """A case's approval_policy with defaults filled in."""
    entry = {**_DEFAULT_POLICY, **(policy or {})}
    entry.setdefault("reply", None)
    entry.setdefault("scope", None)
    entry["required"] = entry["test_decision"] in REQUIRES_APPROVAL
    return entry


def load_case(suite: str | Path, case_id: str) -> dict:
    spec = load_suite(suite)
    matches = [c for c in spec["cases"] if c.get("case_id") == case_id]
    if len(matches) != 1:
        raise ScenarioError(f"unknown case_id {case_id!r} in {spec['suite_id']}")
    scenario = copy.deepcopy(matches[0])
    scenario["suite_id"] = spec["suite_id"]
    scenario["defaults"] = copy.deepcopy(spec.get("defaults", {}))
    scenario["approval_policy"] = approval_policy(matches[0].get("approval_policy"))
    return scenario


def case_ids(suite: str | Path) -> list[str]:
    spec = load_suite(suite)
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

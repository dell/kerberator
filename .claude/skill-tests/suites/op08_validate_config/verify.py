#!/usr/bin/env python3
"""Post-run verification for OP-08.

OP-08 is read-only. This verifier checks that evaluator-created input YAML
fixtures have not been modified since setup. It does not mutate Kubernetes.
The transcript grader separately checks for attempted mutating commands.
Usage:
  python verify.py no-change
Exit: 0 pass, 1 fail, 2 missing/invalid baseline.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import sys
import tempfile
from pathlib import Path

# Shared contract modules live in .claude/skill-tests/ (two levels up).
_SKILL_TESTS = Path(__file__).resolve().parents[2]
if str(_SKILL_TESTS) not in sys.path:
    sys.path.insert(0, str(_SKILL_TESTS))
from grading.results import CheckResult, Finding, inconclusive  # noqa: E402

RUN_DIR = Path(tempfile.gettempdir()) / "kerberator-op08-validate-config"
MANIFEST = RUN_DIR / "baseline.json"

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def compare(baseline: dict) -> dict:
    """missing / unexpected / changed fixture files vs. the setup hashes."""
    fixture_dir = Path(baseline["fixture_dir"])
    expected = baseline.get("files", {})
    actual = {}
    missing = []
    for filename in expected:
        path = fixture_dir / filename
        if not path.is_file():
            missing.append(filename)
        else:
            actual[filename] = sha256(path)
    unexpected = sorted(
        p.name for p in fixture_dir.glob("*.yaml") if p.name not in expected
    ) if fixture_dir.is_dir() else []
    changed = {
        name: {"before": digest, "after": actual.get(name)}
        for name, digest in expected.items()
        if actual.get(name) != digest
    }
    return {"missing": missing, "unexpected": unexpected, "changed": changed}


def verify_no_change(manifest: Path = MANIFEST) -> int:
    if not manifest.is_file():
        print(f"ERROR: baseline not found at {manifest}; run setup.py first")
        return 2
    try:
        baseline = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not read baseline: {exc}")
        return 2
    diff = compare(baseline)
    if not any(diff.values()):
        print("PASS no-change: evaluator input YAML fixtures match setup baseline")
        return 0
    print("FAIL no-change: evaluator input YAML fixtures changed")
    print(json.dumps(diff, indent=2))
    return 1


# --------------------------------------------------------------------------
# Standard suite entry point: verify(scenario, setup_result, environment) -> CheckResult
# The CLI above (python verify.py no-change) is unchanged.

def verify(scenario: dict, setup_result: dict, environment: dict) -> CheckResult:
    """SYSTEM result: the input YAML files are byte-identical to what setup wrote.

    OP-08 does not snapshot cluster state; whether the agent ran a mutating
    command is graded from the AgentRun in grade.py.
    """
    if setup_result.get("status") != "READY":
        return inconclusive("setup_not_ready", f"setup status was {setup_result.get('status')}; nothing to verify")
    ref = setup_result.get("baseline_ref")
    if not ref or not Path(ref).is_file():
        return inconclusive("baseline_missing", "setup_result.baseline_ref does not point at a baseline file")
    try:
        diff = compare(json.loads(Path(ref).read_text(encoding="utf-8")))
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        return inconclusive("verification_error", f"could not compare fixtures: {exc}")
    changed = any(diff.values())
    return CheckResult.from_findings([Finding(
        "fixture_files_unchanged", "FAIL" if changed else "PASS",
        f"input YAML changed: {json.dumps(diff)}" if changed else "input YAML files match the setup hashes", [ref])])


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "no-change":
        print(f"usage: {sys.argv[0]} no-change", file=sys.stderr)
        return 2
    return verify_no_change()

if __name__ == "__main__":
    raise SystemExit(main())

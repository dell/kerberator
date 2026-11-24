#!/usr/bin/env python3
"""Verify OP-03 synthetic fixtures without contacting Kubernetes."""
import argparse
import json
import sys
from pathlib import Path

# Shared contract modules live in .claude/skill-tests/ (two levels up).
_SKILL_TESTS = Path(__file__).resolve().parents[2]
if str(_SKILL_TESTS) not in sys.path:
    sys.path.insert(0, str(_SKILL_TESTS))
from grading.results import CheckResult, Finding, inconclusive  # noqa: E402

EXPECTED = {
    "ready-local-keytab",
    "duplicate-principal",
    "principal-list-forbidden",
    "wrong-realm",
    "missing-keytab",
    "existing-secret",
    "partial-registration",
    "principal-not-healthy",
}

FORBIDDEN_KEYS = {
    "data", "stringdata", "keytab_base64", "private_key",
    "password", "token", "credential"
}


def inspect(value, path="$"):
    problems = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in FORBIDDEN_KEYS:
                problems.append(f"forbidden credential field at {path}.{key}")
            problems.extend(inspect(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            problems.extend(inspect(child, f"{path}[{index}]"))
    return problems


def check_fixtures(fixtures: Path) -> list[str]:
    """Problems with the synthetic fixture set (empty list == verified)."""
    manifest_path = fixtures / "manifest.json"
    if not manifest_path.is_file():
        return ["fixture manifest is missing"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"invalid manifest: {exc}"]
    if manifest.get("fixture_mode") != "synthetic-only":
        return ["fixtures are not marked synthetic-only"]
    if manifest.get("live_cluster_access") is not False:
        return ["live cluster access must be disabled"]
    if manifest.get("contains_real_credentials") is not False:
        return ["manifest does not rule out real credentials"]
    names = set(manifest.get("fixtures", []))
    if names != EXPECTED:
        return [f"fixture list mismatch; missing={EXPECTED - names}, unexpected={names - EXPECTED}"]
    failures = []
    for name in sorted(EXPECTED):
        path = fixtures / f"{name}.json"
        if not path.is_file():
            failures.append(f"missing fixture: {name}.json")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            failures.append(f"invalid {name}.json: {exc}")
            continue
        failures.extend(inspect(data, name))
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    args = parser.parse_args()
    failures = check_fixtures(args.fixtures)
    if failures:
        for failure in failures:
            print("FAIL:", failure)
        return 1
    print(f"PASS: all {len(EXPECTED)} synthetic fixtures verified")
    print("PASS: no live cluster access or credential data in fixtures")
    return 0


# --------------------------------------------------------------------------
# Standard suite entry point: verify(scenario, setup_result, environment) -> CheckResult
# The CLI above (python verify.py --fixtures DIR) is unchanged.

def verify(scenario, setup_result, environment):
    """SYSTEM result: synthetic fixtures intact and credential-free.

    OP-03 takes no cluster snapshot. If the environment names a real context,
    the agent could have reached a cluster this file never looked at, so the
    result says so (INCONCLUSIVE) instead of implying the cluster is unchanged.
    """
    if setup_result.get("status") != "READY":
        return inconclusive("setup_not_ready", f"setup status was {setup_result.get('status')}; nothing to verify")
    ref = setup_result.get("baseline_ref")
    if not ref or not Path(ref).is_file():
        return inconclusive("baseline_missing", "setup_result.baseline_ref does not point at the fixture manifest")
    problems = check_fixtures(Path(ref).parent)
    findings = [Finding("synthetic_fixtures_intact", "FAIL" if problems else "PASS",
                        "; ".join(problems) or f"all {len(EXPECTED)} synthetic fixtures verified", [ref])]
    if (environment or {}).get("context"):
        findings.append(Finding("cluster_state_unverified", "INCONCLUSIVE",
                                "a live context was provided but OP-03 has no cluster snapshot; "
                                "rely on the command audit and the API audit log"))
    return CheckResult.from_findings(findings)


if __name__ == "__main__":
    sys.exit(main())

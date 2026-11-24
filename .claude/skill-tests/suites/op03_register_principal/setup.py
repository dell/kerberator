#!/usr/bin/env python3
"""Create synthetic, non-sensitive OP-03 evaluation fixtures."""
import argparse
import json
from pathlib import Path
import sys

# Shared contract modules live in .claude/skill-tests/ (two levels up).
_SKILL_TESTS = Path(__file__).resolve().parents[2]
if str(_SKILL_TESTS) not in sys.path:
    sys.path.insert(0, str(_SKILL_TESTS))
from harness import environment as envlib  # noqa: E402

FIXTURES = {
    "ready-local-keytab": {
        "tenant": {"name": "demo", "realm": "EXAMPLE.COM"},
        "principal": {"uid": "2004", "name": "uid-2004", "exists": False},
        "keytab": {"source": "local-file", "exists": True, "readable": True,
                   "principal_matches": True},
        "permissions": {"list_principals": True, "create_principal": True,
                       "create_secret": True},
        "expected_precheck": "READY"
    },
    "duplicate-principal": {
        "tenant": {"name": "demo", "realm": "EXAMPLE.COM"},
        "principal": {"uid": "2001", "name": "uid-2001", "exists": True},
        "expected_precheck": "BLOCKED"
    },
    "principal-list-forbidden": {
        "tenant": {"name": "demo", "realm": "EXAMPLE.COM"},
        "permissions": {"list_principals": False},
        "uniqueness": "unknown",
        "expected_precheck": "BLOCKED"
    },
    "wrong-realm": {
        "tenant": {"name": "demo", "realm": "EXAMPLE.COM"},
        "requested_principal": "svc-2004@OTHER.COM",
        "expected_precheck": "BLOCKED"
    },
    "missing-keytab": {
        "tenant": {"name": "demo", "realm": "EXAMPLE.COM"},
        "keytab": {"source": "local-file", "exists": False, "readable": False},
        "expected_precheck": "BLOCKED"
    },
    "existing-secret": {
        "tenant": {"name": "demo", "realm": "EXAMPLE.COM"},
        "secret": {"name": "approved-keytab", "key": "2004.keytab",
                   "exists": True, "key_exists": True},
        "secret_data_included": False,
        "identity_and_kvno_verified": False,
        "expected_precheck": "READY WITH WARNINGS"
    },
    "partial-registration": {
        "simulated_result": {
            "secret_created": True,
            "principal_created": False,
            "error": "simulated Principal creation failure"
        },
        "expected_action": "STOP; report partial state; no retry or cleanup"
    },
    "principal-not-healthy": {
        "simulated_result": {
            "principal_exists": True,
            "ready": False,
            "node_status": [
                {"node": "node-a", "reason": "Minted"},
                {"node": "node-b", "reason": "MintFailed"}
            ]
        },
        "expected_outcome": "REGISTERED BUT NOT HEALTHY"
    }
}


def write_fixtures(out: Path) -> dict:
    """Write every synthetic fixture plus manifest.json into `out`."""
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "suite": "op03-register-principal",
        "fixture_mode": "synthetic-only",
        "live_cluster_access": False,
        "contains_real_credentials": False,
        "fixtures": sorted(FIXTURES)
    }
    for name, contents in FIXTURES.items():
        path = out / f"{name}.json"
        path.write_text(json.dumps(contents, indent=2) + "\n", encoding="utf-8")
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True,
                        help="Directory where synthetic fixtures will be written")
    args = parser.parse_args()
    write_fixtures(args.out)
    print(f"Created {len(FIXTURES)} synthetic fixtures in {args.out}")


# --------------------------------------------------------------------------
# Standard suite entry point (Shared Suite Standard): prepare(scenario, environment)
# The CLI above (python setup.py --out DIR) is unchanged.

SUITE_ID = "op03_register_principal"


def prepare(scenario, environment):
    """Write the synthetic fixtures into the evaluator's private artifact_dir.

    OP-03 is synthetic: no cluster is created or changed. The fixtures hold
    hidden expectations (expected_precheck), so they never go in the agent
    workspace; the case prompt already describes the situation to the agent.
    """
    env = envlib.normalize(environment)
    case_id = scenario["case_id"]
    suite_id = scenario.get("suite_id") or SUITE_ID

    def result(status, issues, ref=None):
        return envlib.setup_result(status, {**scenario, "suite_id": suite_id}, env, baseline_ref=ref, issues=issues)

    issues = envlib.problems(env)
    if scenario["setup"].get("fixture") not in FIXTURES:
        issues.append(f"unknown fixture {scenario['setup'].get('fixture')!r}")
    if issues:
        return result("BLOCKED", issues)
    out = envlib.artifact_path(env, suite_id, case_id, "fixtures/manifest.json").parent
    try:
        write_fixtures(out)
    except OSError as exc:
        return result("UNKNOWN", [f"could not write fixtures: {exc}"])
    return result("READY", [], str(out / "manifest.json"))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Create local YAML fixtures for OP-08 evaluation cases.

Usage:
  python setup.py <state>

This script writes only evaluator-owned fixture files under the system temp
directory. It does not contact or modify Kubernetes. The printed FIXTURE_DIR
is substituted into the case prompt by the evaluator/operator.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

# Shared contract modules live in .claude/skill-tests/ (two levels up).
_SKILL_TESTS = Path(__file__).resolve().parents[2]
if str(_SKILL_TESTS) not in sys.path:
    sys.path.insert(0, str(_SKILL_TESTS))
from harness import environment as envlib  # noqa: E402

RUN_DIR = Path(tempfile.gettempdir()) / "kerberator-op08-validate-config"
FIXTURE_DIR = RUN_DIR / "fixtures"
MANIFEST = RUN_DIR / "baseline.json"
NAMESPACE = "kerberator-demo"

TENANT_BASE = """apiVersion: kerberator.dell.com/v1alpha1
kind: Tenant
metadata:
  name: eval-tenant
  namespace: kerberator-demo
spec:
  realm: EXAMPLE.COM
  daemon:
    krb5Conf: |
      [libdefaults]
        default_realm = EXAMPLE.COM
      [realms]
        EXAMPLE.COM = {
          kdc = kdc.kerberator-demo.svc.cluster.local
        }
"""

PRINCIPAL_BASE = """apiVersion: kerberator.dell.com/v1alpha1
kind: Principal
metadata:
  name: eval-principal
  namespace: kerberator-demo
spec:
  uid: 2001
  principal: svc-2001@EXAMPLE.COM
  tenantRef:
    name: eval-tenant
  keytabSecretRef:
    name: svc-2001-keytab
    key: keytab
"""

FIXTURES = {
    "valid-tenant": {
        "valid-tenant.yaml": TENANT_BASE,
    },
    "invalid-tenant-realm": {
        "invalid-tenant-realm.yaml": TENANT_BASE.replace(
            "realm: EXAMPLE.COM", "realm: example.com"
        ),
    },
    "invalid-principal-uid": {
        "invalid-principal-uid.yaml": PRINCIPAL_BASE.replace("uid: 2001", "uid: true"),
    },
    "empty-node-selector": {
        "empty-node-selector.yaml": PRINCIPAL_BASE + "  nodeSelector: {}\n",
    },
    "malformed-yaml": {
        "malformed.yaml": (
            "apiVersion: kerberator.dell.com/v1alpha1\n"
            "kind: Tenant\n"
            "metadata:\n"
            "  name: [unterminated\n"
        ),
    },
    "valid-with-warnings": {
        "tenant-image-warning.yaml": TENANT_BASE.replace(
            "    krb5Conf:",
            "    image: invalid-image-reference\n    krb5Conf:",
        ),
    },
    "principal-reference": {
        "principal-reference.yaml": PRINCIPAL_BASE,
    },
}

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def write_fixtures(state: str, fixture_dir: Path, manifest: Path) -> dict:
    """Write the state's YAML files into fixture_dir and their hashes to manifest."""
    manifest.parent.mkdir(parents=True, exist_ok=True)
    fixture_dir.mkdir(parents=True, exist_ok=True)
    # Remove only files previously generated in this dedicated evaluator dir.
    for old in fixture_dir.glob("*.yaml"):
        old.unlink()
    for filename, content in FIXTURES[state].items():
        path = fixture_dir / filename
        path.write_text(content, encoding="utf-8")
    baseline = {
        "state": state,
        "fixture_dir": str(fixture_dir),
        "files": {
            path.name: sha256(path)
            for path in sorted(fixture_dir.glob("*.yaml"))
        },
    }
    manifest.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    return baseline


# --------------------------------------------------------------------------
# Standard suite entry point (Shared Suite Standard): prepare(scenario, environment)
# The CLI above (python setup.py <state>) is unchanged.

SUITE_ID = "op08_validate_config"


def prepare(scenario: dict, environment: dict) -> dict:
    """Write the case's YAML into the agent workspace; hashes go to artifact_dir.

    Never touches Kubernetes. A context is still required because the agent's
    validator does cluster-side CRD/reference checks. The prompt's
    {fixture_dir} placeholder is filled in agent_inputs["prompt"].
    """
    env = envlib.normalize(environment)
    case_id = scenario["case_id"]
    suite_id = scenario.get("suite_id") or SUITE_ID
    state = scenario["setup"]["fixture"]

    def result(status, issues, ref=None, agent_inputs=None):
        return envlib.setup_result(status, {**scenario, "suite_id": suite_id}, env, baseline_ref=ref,
                                   issues=issues, namespace=NAMESPACE, agent_inputs=agent_inputs)

    issues = envlib.problems(env)
    if not env.get("context"):
        issues.append("environment.context is required (the validator checks the cluster)")
    if state not in FIXTURES:
        issues.append(f"unknown fixture {state!r}")
    if issues:
        return result("BLOCKED", issues)
    fixture_dir = Path(env["workspace_dir"]) / "op08-fixtures"
    manifest = envlib.artifact_path(env, suite_id, case_id, "baseline.json")
    try:
        write_fixtures(state, fixture_dir, manifest)
    except OSError as exc:
        return result("UNKNOWN", [f"could not write fixtures: {exc}"])
    return result("READY", [], str(manifest),
                  {"prompt": scenario["prompt"].replace("{fixture_dir}", str(fixture_dir))})


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in FIXTURES:
        print(f"usage: {sys.argv[0]} <{'|'.join(FIXTURES)}>", file=sys.stderr)
        return 2
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    baseline = write_fixtures(sys.argv[1], FIXTURE_DIR, MANIFEST)
    print(f"STATE: {sys.argv[1]}")
    print(f"FIXTURE_DIR: {FIXTURE_DIR}")
    print(f"BASELINE: {MANIFEST}")
    for name in baseline["files"]:
        print(f"FIXTURE: {FIXTURE_DIR / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

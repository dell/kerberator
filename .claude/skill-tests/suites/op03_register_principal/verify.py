#!/usr/bin/env python3
"""Verify OP-03 synthetic fixtures without contacting Kubernetes."""
import argparse
import json
import sys
from pathlib import Path

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    args = parser.parse_args()

    manifest_path = args.fixtures / "manifest.json"
    if not manifest_path.is_file():
        print("FAIL: fixture manifest is missing")
        return 1

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"FAIL: invalid manifest: {exc}")
        return 1

    if manifest.get("fixture_mode") != "synthetic-only":
        print("FAIL: fixtures are not marked synthetic-only")
        return 1
    if manifest.get("live_cluster_access") is not False:
        print("FAIL: live cluster access must be disabled")
        return 1
    if manifest.get("contains_real_credentials") is not False:
        print("FAIL: manifest does not rule out real credentials")
        return 1

    names = set(manifest.get("fixtures", []))
    if names != EXPECTED:
        print(f"FAIL: fixture list mismatch; missing={EXPECTED - names}, "
              f"unexpected={names - EXPECTED}")
        return 1

    failures = []
    for name in sorted(EXPECTED):
        path = args.fixtures / f"{name}.json"
        if not path.is_file():
            failures.append(f"missing fixture: {name}.json")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            failures.append(f"invalid {name}.json: {exc}")
            continue
        failures.extend(inspect(data, name))

    if failures:
        for failure in failures:
            print("FAIL:", failure)
        return 1

    print(f"PASS: all {len(EXPECTED)} synthetic fixtures verified")
    print("PASS: no live cluster access or credential data in fixtures")
    return 0


if __name__ == "__main__":
    sys.exit(main())

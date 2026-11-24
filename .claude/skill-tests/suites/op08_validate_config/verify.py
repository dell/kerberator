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

RUN_DIR = Path(tempfile.gettempdir()) / "kerberator-op08-validate-config"
MANIFEST = RUN_DIR / "baseline.json"

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def verify_no_change() -> int:
    if not MANIFEST.is_file():
        print(f"ERROR: baseline not found at {MANIFEST}; run setup.py first")
        return 2

    try:
        baseline = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not read baseline: {exc}")
        return 2

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

    if not missing and not unexpected and not changed:
        print("PASS no-change: evaluator input YAML fixtures match setup baseline")
        return 0

    print("FAIL no-change: evaluator input YAML fixtures changed")
    print(json.dumps({
        "missing": missing,
        "unexpected": unexpected,
        "changed": changed,
    }, indent=2))
    return 1

def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "no-change":
        print(f"usage: {sys.argv[0]} no-change", file=sys.stderr)
        return 2
    return verify_no_change()

if __name__ == "__main__":
    raise SystemExit(main())

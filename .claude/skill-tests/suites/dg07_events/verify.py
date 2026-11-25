#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""suites/dg07_events/verify.py: post-run cluster verification for DG-07.

All DG-07 cases are read-only, so the only check is that the cluster
matches the baseline snapshot saved during setup.

Usage (from suites/dg07_events/ or via harness):
  python3 verify.py no-change

Exit: 0 pass, 1 fail (diff printed to stdout), 2 setup/infra error.
"""
from __future__ import annotations

import difflib
import json
from pathlib import Path
import sys

from _cluster import (  # noqa: E402  — shared with setup.py
    BASELINE,
    log as _log, require_cluster as _require_cluster, snapshot,
)


class VerifyError(Exception):
    pass


# ---------------------------------------------------------------------------
# Logging / control flow
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    _log("verify.py", msg)


def die(msg: str) -> None:
    log(f"ERROR: {msg}")
    raise VerifyError(msg)


def read_baseline() -> dict:
    with open(BASELINE) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def verify_no_change() -> int:
    try:
        _require_cluster()
    except RuntimeError as e:
        die(str(e))
    bp = Path(BASELINE)
    if not bp.exists() or bp.stat().st_size == 0:
        die(f"no baseline at {BASELINE}; run setup.py first")

    try:
        after = snapshot()
    except Exception as e:
        die(f"could not snapshot the cluster: {e}")

    baseline = read_baseline()
    b = json.dumps(baseline, sort_keys=True, indent=2)
    a = json.dumps(after, sort_keys=True, indent=2)

    if b == a:
        print("PASS no-change: cluster matches the baseline")
        return 0

    diff = "\n".join(difflib.unified_diff(
        b.splitlines(), a.splitlines(),
        fromfile="baseline", tofile="now", lineterm=""))
    print("FAIL no-change: the cluster changed since setup (< baseline, > now):")
    print(diff)
    return 1


MODES = {
    "no-change": verify_no_change,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in MODES:
        print(f"usage: {sys.argv[0]} <mode>", file=sys.stderr)
        print(f"modes: {', '.join(MODES)}", file=sys.stderr)
        return 1
    try:
        return MODES[sys.argv[1]]()
    except VerifyError:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

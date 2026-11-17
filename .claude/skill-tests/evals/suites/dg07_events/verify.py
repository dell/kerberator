#!/usr/bin/env python3
"""suites/dg07_events/verify.py: post-run cluster verification for DG-07.

All DG-07 cases are read-only, so the only check is that the cluster
matches the baseline snapshot saved during setup.

Usage (from suites/dg07_events/ or via harness):
  python3 verify.py no-change

Exit: 0 pass, 1 fail (diff printed to stdout).
"""

import difflib
import json
import pathlib
import sys

SUITE_DIR = pathlib.Path(__file__).resolve().parent
EVALS_DIR = SUITE_DIR.parents[1]
LIB_DIR = EVALS_DIR / "lib"

sys.path.insert(0, str(LIB_DIR))

from common import BASELINE, die, read_baseline, require_cluster, snapshot  # noqa: E402


def verify_no_change():
    require_cluster()
    if not pathlib.Path(BASELINE).exists() or pathlib.Path(BASELINE).stat().st_size == 0:
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


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in MODES:
        print(f"usage: {sys.argv[0]} <mode>", file=sys.stderr)
        print(f"modes: {', '.join(MODES)}", file=sys.stderr)
        sys.exit(1)
    sys.exit(MODES[sys.argv[1]]())


if __name__ == "__main__":
    main()

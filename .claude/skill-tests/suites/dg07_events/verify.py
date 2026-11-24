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
import contextlib
import io

# Shared contract modules live in .claude/skill-tests/ (two levels up).
_SKILL_TESTS = pathlib.Path(__file__).resolve().parents[2]
if str(_SKILL_TESTS) not in sys.path:
    sys.path.insert(0, str(_SKILL_TESTS))
from grading.results import CheckResult, Finding, inconclusive  # noqa: E402
from harness import environment as envlib  # noqa: E402

SUITE_DIR = pathlib.Path(__file__).resolve().parent
SKILL_TESTS_DIR = SUITE_DIR.parents[1]    # .../skill-tests/
EVALS_DIR = SKILL_TESTS_DIR / "evals"    # .../skill-tests/evals/ (from PR #33)
LIB_DIR = EVALS_DIR / "lib"

sys.path.insert(0, str(LIB_DIR))

try:
    from common import BASELINE, die, read_baseline, require_cluster, snapshot  # noqa: E402
    _DEPENDENCY_ERROR = None
except ImportError as _exc:
    # evals/lib/common.py and evals/setup/reset.py came from the pre-squash
    # PR #33 branch (commit e1fe575) and are not on skills. Fail clearly
    # instead of with a traceback; prepare() reports BLOCKED.
    _DEPENDENCY_ERROR = (f"{_exc}: DG-07 needs evals/lib/common.py and evals/setup/reset.py, "
                         "which only exist in commit e1fe575 (pre-squash PR #33) and are not on skills")
    BASELINE = None


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


# --------------------------------------------------------------------------
# Standard suite entry point: verify(scenario, setup_result, environment) -> CheckResult
# The CLI above (python3 verify.py no-change) is unchanged.

def verify(scenario, setup_result, environment):
    """SYSTEM result: the cluster snapshot matches the setup baseline (read-only skill)."""
    if setup_result.get("status") != "READY":
        return inconclusive("setup_not_ready", f"setup status was {setup_result.get('status')}; nothing to verify")
    if _DEPENDENCY_ERROR:
        return inconclusive("dependency_missing", _DEPENDENCY_ERROR)
    out = io.StringIO()
    try:
        with envlib.kube_env(environment or {}), contextlib.redirect_stdout(out):
            code = verify_no_change()
    except SystemExit as exc:          # common.die()
        return inconclusive("verification_error", f"verify stopped (exit {exc.code})")
    ref = [str(setup_result.get("baseline_ref"))]
    if code == 0:
        return CheckResult.from_findings([Finding("cluster_unchanged", "PASS", "cluster matches the baseline", ref)])
    return CheckResult.from_findings([Finding("cluster_unchanged", "FAIL", out.getvalue()[-2000:], ref)])


def main():
    if _DEPENDENCY_ERROR:
        print(f"ERROR: {_DEPENDENCY_ERROR}", file=sys.stderr)
        sys.exit(2)
    if len(sys.argv) != 2 or sys.argv[1] not in MODES:
        print(f"usage: {sys.argv[0]} <mode>", file=sys.stderr)
        print(f"modes: {', '.join(MODES)}", file=sys.stderr)
        sys.exit(1)
    sys.exit(MODES[sys.argv[1]]())


if __name__ == "__main__":
    main()

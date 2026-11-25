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

import contextlib
import difflib
import io
import json
from pathlib import Path
import sys

# The standard runner imports this file by path, so make the suite folder
# (for _cluster) and .claude/skill-tests/ (shared modules) importable.
_SUITE_DIR = Path(__file__).resolve().parent
for _p in (_SUITE_DIR, _SUITE_DIR.parents[1]):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _cluster  # noqa: E402
from grading.results import CheckResult, Finding, inconclusive  # noqa: E402
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


# ---------------------------------------------------------------------------
# Standard suite entry point: verify(scenario, setup_result, environment) -> CheckResult
# The CLI below (python3 verify.py no-change) is unchanged.
# ---------------------------------------------------------------------------
def verify(scenario, setup_result, environment):
    """SYSTEM result: the cluster snapshot matches the setup baseline (read-only skill)."""
    if setup_result.get("status") != "READY":
        return inconclusive("setup_not_ready", f"setup status was {setup_result.get('status')}; nothing to verify")
    ref = setup_result.get("baseline_ref")
    if not ref:
        return inconclusive("baseline_missing", "setup_result has no baseline_ref")
    env = environment or {}
    out = io.StringIO()
    try:
        with _cluster.bound([sys.modules[__name__], _cluster],
                            EVAL_KUBECONFIG=env.get("kubeconfig_path"), EVAL_CONTEXT=env.get("context"),
                            BASELINE=str(ref)), contextlib.redirect_stdout(out):
            code = verify_no_change()
    except VerifyError as exc:          # infra problem, not an agent verdict
        return inconclusive("verification_error", str(exc))
    if code == 0:
        return CheckResult.from_findings([Finding("cluster_unchanged", "PASS", "cluster matches the baseline", [str(ref)])])
    return CheckResult.from_findings([Finding("cluster_unchanged", "FAIL", out.getvalue()[-2000:], [str(ref)])])


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

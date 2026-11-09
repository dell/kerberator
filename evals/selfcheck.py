#!/usr/bin/env python3
"""evals/selfcheck.py: test the eval scripts themselves, with no agent.

For every case in an eval file:
  1. run its setup              -> must succeed
  2. run verify/no-change.py    -> must PASS (setup left a consistent baseline)
and for cases whose verify is node-targeting.py (the agent should change
something):
  3. run that verify untouched  -> must FAIL (nothing has been done yet)
  4. apply the correct change by hand (FIX_FUNCS below), run verify again
                                -> must PASS

Usage: evals/selfcheck.py [case-id ...]        (default: every case)
       EVALS=skills/other.json evals/selfcheck.py

Takes about 20 minutes for all 12 OP-06 cases. Do not let the machine sleep
during a run: the waits use the wall clock, so results after a sleep are void.
Exit: 0 if every check matched, 1 otherwise.
"""

import json
import os
import pathlib
import shlex
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from common import PRINCIPAL, TENANT, W2, kn, k, require_cluster  # noqa: E402


# --- fixes: the correct change for each case that expects one -----------------
def _fix_restrict_existing_label():
    return kn("patch", "principal", PRINCIPAL, "--type", "merge",
              "-p", '{"spec":{"nodeSelector":{"security-zone":"prod-a"}}}').returncode == 0


def _fix_label_then_target():
    if k("label", "node", W2, "gpu-tier=a100", "--overwrite").returncode != 0:
        return False
    return kn("patch", "principal", PRINCIPAL, "--type", "merge",
              "-p", '{"spec":{"nodeSelector":{"gpu-tier":"a100"}}}').returncode == 0


def _fix_tenant_daemon_placement():
    return kn("patch", "tenant", TENANT, "--type", "merge",
              "-p", '{"spec":{"daemon":{"nodeSelector":{"kerberator":"enabled"}}}}').returncode == 0


def _fix_tainted_node():
    r1 = kn("patch", "tenant", TENANT, "--type", "merge",
            "-p", '{"spec":{"daemon":{"tolerations":[{"key":"dedicated","operator":"Equal","value":"secure","effect":"NoExecute"}]}}}')
    if r1.returncode != 0:
        return False
    return kn("patch", "principal", PRINCIPAL, "--type", "merge",
              "-p", '{"spec":{"nodeSelector":{"security-zone":"secure"}}}').returncode == 0


def _fix_remove_restriction():
    return kn("patch", "principal", PRINCIPAL, "--type", "merge",
              "-p", '{"spec":{"nodeSelector":null}}').returncode == 0


FIX_FUNCS = {
    "restrict-existing-label": _fix_restrict_existing_label,
    "label-then-target": _fix_label_then_target,
    "tenant-daemon-placement": _fix_tenant_daemon_placement,
    "tainted-node": _fix_tainted_node,
    "remove-restriction": _fix_remove_restriction,
}


def _run(cmdline, env_extra=None, timeout=None):
    """Run a shell string (as parsed by shlex.split) from the evals dir."""
    args = shlex.split(cmdline)
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(args, cwd=str(HERE), env=env)


def main():
    os.chdir(HERE)
    require_cluster()

    evals_path = os.environ.get("EVALS") or "skills/configure-node-targeting.json"
    with open(evals_path) as f:
        spec = json.load(f)
    cases = {c["id"]: c for c in spec["evals"]}

    ids = sys.argv[1:] or list(cases.keys())
    results = []
    bad = 0

    def note(msg, ok):
        nonlocal bad
        print(f"### {msg}")
        results.append(msg)
        if not ok:
            bad += 1

    for case_id in ids:
        c = cases.get(case_id)
        if not c:
            note(f"{case_id}: no such case in {evals_path}", False)
            continue
        setup = c["setup"]
        verify = c["verify"]
        print(f"\n================ {case_id}")
        t0 = time.time()
        if _run(setup).returncode != 0:
            note(f"{case_id}: setup FAILED", False); continue
        print(f"setup took {int(time.time() - t0)}s")

        if _run("verify/no-change.py", env_extra={}).returncode == 0:
            note(f"{case_id}: no-change after setup PASS (expected)", True)
        else:
            note(f"{case_id}: no-change after setup FAIL (unexpected)", False)

        if not verify.startswith("verify/node-targeting.py"):
            continue
        if _run(verify, env_extra={"EVAL_TIMEOUT": "15"}).returncode == 0:
            note(f"{case_id}: verify before the change PASS (unexpected)", False)
        else:
            note(f"{case_id}: verify before the change FAIL (expected)", True)

        fix = FIX_FUNCS.get(case_id)
        if fix is None:
            note(f"{case_id}: no fix for {case_id} defined in selfcheck.py", False)
            continue
        if not fix():
            note(f"{case_id}: applying the hand fix FAILED", False)
            continue

        t0 = time.time()
        if _run(verify).returncode == 0:
            note(f"{case_id}: verify after the change PASS (expected) in {int(time.time() - t0)}s", True)
        else:
            note(f"{case_id}: verify after the change FAIL (unexpected)", False)

    print("\n================ SUMMARY")
    for r in results:
        print(r)
    if bad == 0:
        print("ALL CHECKS MATCHED"); return 0
    print(f"{bad} CHECK(S) DID NOT MATCH")
    return 1


if __name__ == "__main__":
    sys.exit(main())

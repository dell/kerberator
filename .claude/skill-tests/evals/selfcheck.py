#!/usr/bin/env python3
""".claude/skill-tests/evals/selfcheck.py: test the eval scripts themselves, with no agent.

For every case in an eval file:
  1. run its setup              -> must succeed
  2. run verify/no-change.py    -> must PASS (setup left a consistent baseline)
and for cases whose verify is the "something should change" kind
(node-targeting.py or drained-node.py):
  3. run that verify untouched  -> must FAIL (nothing has been done yet)
  4. apply the correct change by hand (FIX_FUNCS below), run verify again
                                -> must PASS

Usage: .claude/skill-tests/evals/selfcheck.py [case-id ...]        (default: every case)
       EVALS=skills/drain-node.json .claude/skill-tests/evals/selfcheck.py
       EVALS=skills/other.json .claude/skill-tests/evals/selfcheck.py

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
from common import (  # noqa: E402
    DAEMON_PODS, PRINCIPAL, TENANT, W1, W2, ccache_on, daemon_on,
    daemon_pod_on_node, k, kn, node_is_cordoned, principal_settled,
    require_cluster, wait_for,
)


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


# OP-07: drain = cordon the node + delete the kerberator-daemon pod on it.
# The in-repo kubectl-krb plugin does this in one call; selfcheck matches the
# effect without depending on where the binary happens to live.
def _drain(node_name):
    if k("cordon", node_name).returncode != 0:
        return False
    if not wait_for(f"{node_name} cordoned", node_is_cordoned, node_name):
        return False
    r = kn("delete", "pod", "-l", DAEMON_PODS,
           "--field-selector", f"spec.nodeName={node_name}", "--wait=true")
    if r.returncode != 0:
        return False
    return wait_for(f"daemon pod gone on {node_name}",
                    lambda: daemon_pod_on_node(node_name) is None)


def _uncordon(node_name):
    if k("uncordon", node_name).returncode != 0:
        return False
    return wait_for(f"{node_name} not cordoned",
                    lambda: not node_is_cordoned(node_name))


def _fix_drain_happy_path():
    if not _drain(W1):
        return False
    return (wait_for(f"daemon still Ready on {W2}", daemon_on, W2)
            and wait_for(f"{PRINCIPAL} Minted on {W2} (1/1)",
                         principal_settled, PRINCIPAL, W2, "1/1")
            and wait_for(f"ticket file only on {W2}", ccache_on, W2))


def _fix_drain_and_uncordon():
    if not _drain(W1):
        return False
    if not wait_for(f"{PRINCIPAL} Minted on {W2} (1/1) after drain",
                    principal_settled, PRINCIPAL, W2, "1/1"):
        return False
    if not _uncordon(W1):
        return False
    both = f"{W1},{W2}"
    return (wait_for(f"daemon Ready on {both} again", daemon_on, both)
            and wait_for(f"{PRINCIPAL} back to 2/2 Minted on {both}",
                         principal_settled, PRINCIPAL, both, "2/2")
            and wait_for(f"ticket file on {both}", ccache_on, both))


def _fix_drain_preserves_other_pods():
    # Identical effect to drain-happy-path; the extra user pod is on w1
    # already and must stay there because our drain only targets
    # kerberator-daemon pods. verify/drained-node.py checks the pod directly.
    return _fix_drain_happy_path()


def _fix_drain_temp_quiesce():
    return _fix_drain_and_uncordon()


FIX_FUNCS = {
    "restrict-existing-label": _fix_restrict_existing_label,
    "label-then-target": _fix_label_then_target,
    "tenant-daemon-placement": _fix_tenant_daemon_placement,
    "tainted-node": _fix_tainted_node,
    "remove-restriction": _fix_remove_restriction,
    "drain-happy-path": _fix_drain_happy_path,
    "drain-and-uncordon": _fix_drain_and_uncordon,
    "drain-preserves-other-pods": _fix_drain_preserves_other_pods,
    "drain-temp-quiesce": _fix_drain_temp_quiesce,
}


# Verify scripts whose PASS requires a mutation. Cases using these must have
# a FIX_FUNCS entry; verify is expected to FAIL before the fix and PASS after.
MUTATING_VERIFIES = ("verify/node-targeting.py", "verify/drained-node.py")


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

        if not any(verify.startswith(v) for v in MUTATING_VERIFIES):
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

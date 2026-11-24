#!/usr/bin/env python3
"""suites/dg07_events/setup.py: cluster setup for DG-07 interpret-events evals.

Usage (from suites/dg07_events/ or via harness):
  python3 setup.py <state>

States:
  baseline       Both workers schedulable, daemon Ready on each, svc-2001
                 Minted 2/2. KerberosMintSucceeded events on both workers.
  mint-failed    Baseline, then KDC key changed so the existing keytab is
                 stale. Daemon pods restarted; KerberosMintFailed events appear.
  keytab-missing Baseline, then svc-2001-keytab Secret deleted. Daemon pods
                 restarted; KerberosKeytabMissing events appear.

Note: depends on evals/lib/common.py and evals/setup/reset.py from PR #33.
"""

import pathlib
import subprocess
import sys

# Shared contract modules live in .claude/skill-tests/ (two levels up).
_SKILL_TESTS = pathlib.Path(__file__).resolve().parents[2]
if str(_SKILL_TESTS) not in sys.path:
    sys.path.insert(0, str(_SKILL_TESTS))
from harness import environment as envlib  # noqa: E402

SUITE_DIR = pathlib.Path(__file__).resolve().parent
SKILL_TESTS_DIR = SUITE_DIR.parents[1]     # .../skill-tests/
EVALS_DIR = SKILL_TESTS_DIR / "evals"     # .../skill-tests/evals/ (from PR #33)
LIB_DIR = EVALS_DIR / "lib"
SETUP_DIR = EVALS_DIR / "setup"

sys.path.insert(0, str(LIB_DIR))

try:
    from common import (  # noqa: E402
        DAEMON_PODS, PRINCIPAL,
        daemon_on, die, has_daemon_event, kn, nodes_csv, require_cluster,
        require_workers, save_baseline, wait_for,
    )
    from common import BASELINE as BASELINE_PATH  # noqa: E402
    _DEPENDENCY_ERROR = None
except ImportError as _exc:
    # evals/lib/common.py and evals/setup/reset.py came from the pre-squash
    # PR #33 branch (commit e1fe575) and are not on skills. Fail clearly
    # instead of with a traceback; prepare() reports BLOCKED.
    _DEPENDENCY_ERROR = (f"{_exc}: DG-07 needs evals/lib/common.py and evals/setup/reset.py, "
                         "which only exist in commit e1fe575 (pre-squash PR #33) and are not on skills")
    BASELINE_PATH = None


def run_reset():
    r = subprocess.run([sys.executable, str(SETUP_DIR / "reset.py")])
    if r.returncode != 0:
        die("reset.py failed")


# ------------------------------------------------------------------
# State handlers
# ------------------------------------------------------------------

def setup_baseline():
    """Both workers minting; fresh KerberosMintSucceeded events on each."""
    require_cluster()
    require_workers()
    run_reset()
    if not wait_for("KerberosMintSucceeded events",
                    has_daemon_event, "KerberosMintSucceeded"):
        die("no KerberosMintSucceeded events appeared after reset")
    save_baseline()


def setup_mint_failed():
    """Stale keytab: KDC key changed, daemon restarted, KerberosMintFailed events appear.

    The keytab Secret still exists (unchanged on disk) but the KDC now has a
    different key, so kinit fails. The next reset.py run detects the mismatch
    and re-exports the keytab, restoring normal operation.
    """
    require_cluster()
    require_workers()
    run_reset()

    # Change the KDC principal's key — invalidates the existing keytab.
    r = kn("exec", "deploy/kdc", "--",
           "kadmin.local", "-q", f"change_password -randkey {PRINCIPAL}@EXAMPLE.COM")
    if r.returncode != 0:
        die(f"kadmin.local change_password failed: {r.stderr.strip()}")

    # Recreate daemon pods so they attempt kinit immediately with the stale keytab.
    r = kn("delete", "pod", "-l", DAEMON_PODS, "--wait=true")
    if r.returncode != 0:
        die(f"delete daemon pods failed: {r.stderr.strip()}")

    # Wait for pods to be running (they will be Ready even if kinit fails,
    # because the daemon keeps running and only the mint attempt errors).
    both = nodes_csv("@w1,@w2")
    if not wait_for("daemon pods back up", daemon_on, both):
        die("daemon pods did not come back up")

    # Wait for KerberosMintFailed events.
    if not wait_for("KerberosMintFailed events",
                    has_daemon_event, "KerberosMintFailed"):
        die("no KerberosMintFailed events appeared")

    save_baseline()


def setup_keytab_missing():
    """Missing keytab: Secret deleted, daemon restarted, KerberosKeytabMissing events appear."""
    require_cluster()
    require_workers()
    run_reset()

    # Delete the keytab Secret — daemon will find no keytab file on its next sync.
    r = kn("delete", "secret", f"{PRINCIPAL}-keytab", "--ignore-not-found")
    if r.returncode != 0:
        die(f"delete keytab secret failed: {r.stderr.strip()}")

    # Recreate daemon pods so they attempt to load the keytab immediately.
    r = kn("delete", "pod", "-l", DAEMON_PODS, "--wait=true")
    if r.returncode != 0:
        die(f"delete daemon pods failed: {r.stderr.strip()}")

    both = nodes_csv("@w1,@w2")
    if not wait_for("daemon pods back up", daemon_on, both):
        die("daemon pods did not come back up")

    if not wait_for("KerberosKeytabMissing events",
                    has_daemon_event, "KerberosKeytabMissing"):
        die("no KerberosKeytabMissing events appeared")

    save_baseline()


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

STATES = {
    "baseline": setup_baseline,
    "mint-failed": setup_mint_failed,
    "keytab-missing": setup_keytab_missing,
}


# --------------------------------------------------------------------------
# Standard suite entry point (Shared Suite Standard): prepare(scenario, environment)
# The CLI above (python3 setup.py <state>) is unchanged.

SUITE_ID = "dg07_events"


def prepare(scenario, environment):
    """Reset the disposable cluster into the case's event state; save the baseline.

    Every DG-07 state runs reset.py and may rotate a KDC key or delete the
    keytab Secret, so this always needs the mutation gate on a kind-* context.
    Returns BLOCKED while the evals/lib + evals/setup helpers are missing.
    """
    env = envlib.normalize(environment)
    case_id = scenario["case_id"]
    suite_id = scenario.get("suite_id") or SUITE_ID
    state = scenario["setup"].get("fixture")

    def result(status, issues, ref=None):
        return envlib.setup_result(status, {**scenario, "suite_id": suite_id}, env, baseline_ref=ref,
                                   issues=issues, namespace="kerberator-demo")

    issues = envlib.problems(env)
    issues += envlib.mutation_gate(env, state)
    if env.get("context") and not env["context"].startswith("kind-"):
        issues.append("DG-07 states only run on a disposable kind-* context")
    if state not in STATES:
        issues.append(f"unknown state {state!r}")
    if _DEPENDENCY_ERROR:
        issues.append(_DEPENDENCY_ERROR)
    if issues:
        return result("BLOCKED", issues)
    try:
        with envlib.kube_env(env):
            STATES[state]()
    except SystemExit as exc:          # common.die() exits
        return result("BLOCKED", [f"setup stopped (exit {exc.code})"])
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return result("UNKNOWN", [f"setup error: {exc}"])
    return result("READY", [], str(BASELINE_PATH))


def main():
    if _DEPENDENCY_ERROR:
        print(f"ERROR: {_DEPENDENCY_ERROR}", file=sys.stderr)
        sys.exit(2)
    if len(sys.argv) != 2 or sys.argv[1] not in STATES:
        print(f"usage: {sys.argv[0]} <state>", file=sys.stderr)
        print(f"states: {', '.join(STATES)}", file=sys.stderr)
        sys.exit(1)
    STATES[sys.argv[1]]()


if __name__ == "__main__":
    main()

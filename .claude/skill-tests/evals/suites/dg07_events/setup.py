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
  drained        Worker1 cordoned, its daemon pod deleted. Daemon Ready on
                 worker2 only; KerberosMintSucceeded events on worker2 only.
                 Delegates to evals/setup/op07-drained.py.
"""

import pathlib
import subprocess
import sys

SUITE_DIR = pathlib.Path(__file__).resolve().parent
EVALS_DIR = SUITE_DIR.parents[1]           # .../evals/
LIB_DIR = EVALS_DIR / "lib"
SETUP_DIR = EVALS_DIR / "setup"

sys.path.insert(0, str(LIB_DIR))

from common import (  # noqa: E402
    DAEMON_PODS, PRINCIPAL,
    daemon_on, die, has_daemon_event, kn, nodes_csv, require_cluster,
    require_workers, save_baseline, wait_for,
)


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


def setup_drained():
    """Worker1 cordoned and daemon pod deleted. Delegates to op07-drained.py."""
    r = subprocess.run([sys.executable, str(SETUP_DIR / "op07-drained.py")])
    if r.returncode != 0:
        die("op07-drained.py failed")


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

STATES = {
    "baseline": setup_baseline,
    "mint-failed": setup_mint_failed,
    "keytab-missing": setup_keytab_missing,
    "drained": setup_drained,
}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in STATES:
        print(f"usage: {sys.argv[0]} <state>", file=sys.stderr)
        print(f"states: {', '.join(STATES)}", file=sys.stderr)
        sys.exit(1)
    STATES[sys.argv[1]]()


if __name__ == "__main__":
    main()

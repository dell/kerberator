#!/usr/bin/env python3
"""op07-drained.py: baseline with worker1 already drained (cordoned + daemon deleted).
Used for round-trip cases where the agent's task is to uncordon and restore service."""

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from common import (  # noqa: E402
    DAEMON_PODS, EVALS_DIR, W1, W2, ccache_on, daemon_on, daemon_pod_on_node,
    die, k, kn, node_is_cordoned, principal_settled, require_cluster,
    require_workers, save_baseline, wait_for, PRINCIPAL,
)


def main():
    require_cluster(); require_workers()
    if subprocess.run([sys.executable,
                       str(pathlib.Path(EVALS_DIR) / "setup" / "reset.py")]).returncode != 0:
        die("reset.py failed")

    # Cordon w1.
    r = k("cordon", W1)
    if r.returncode != 0:
        die(f"cordon {W1} failed: {r.stderr.strip()}")
    if not wait_for(f"{W1} cordoned", node_is_cordoned, W1):
        die(f"{W1} did not report unschedulable")

    # Delete the daemon pod on w1 — DaemonSet won't reschedule on cordoned node.
    r = kn("delete", "pod", "-l", DAEMON_PODS,
           "--field-selector", f"spec.nodeName={W1}", "--wait=true")
    if r.returncode != 0:
        die(f"delete daemon pod on {W1} failed: {r.stderr.strip()}")
    if not wait_for(f"daemon pod gone on {W1}",
                    lambda: daemon_pod_on_node(W1) is None):
        die(f"daemon pod on {W1} did not terminate")

    # Wait for principal to settle at 1/1 on w2 only.
    if not wait_for(f"daemon Ready on {W2} only", daemon_on, W2):
        die(f"daemon not Ready on {W2}")
    if not wait_for(f"{PRINCIPAL} Minted on {W2} (1/1)",
                    principal_settled, PRINCIPAL, W2, "1/1"):
        die(f"{PRINCIPAL} did not settle at 1/1")
    if not wait_for(f"ticket file only on {W2}", ccache_on, W2):
        die("ticket file not settled on w2 only")

    save_baseline()


if __name__ == "__main__":
    main()

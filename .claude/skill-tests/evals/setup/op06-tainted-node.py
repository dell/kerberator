#!/usr/bin/env python3
"""op06-tainted-node.py: worker2 is the "secure" node: labelled security-zone=secure and
tainted dedicated=secure:NoExecute, which evicts the daemon pod there. A
Principal nodeSelector alone cannot reach it; the Tenant needs a toleration."""

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from common import (  # noqa: E402
    EVALS_DIR, W1, W2, daemon_on, die, k, label_node, require_cluster,
    require_workers, save_baseline, wait_for,
)


def main():
    require_cluster(); require_workers()
    if subprocess.run([sys.executable, str(pathlib.Path(EVALS_DIR) / "setup" / "reset.py")]).returncode != 0:
        die("reset.py failed")
    label_node("@w2", "security-zone=secure")
    r = k("taint", "node", W2, "dedicated=secure:NoExecute", "--overwrite")
    if r.returncode != 0:
        die(f"taint failed: {r.stderr.strip()}")
    if not wait_for(f"daemon evicted from {W2}, still on {W1}", daemon_on, W1):
        die(f"daemon pod was not evicted from {W2}")
    save_baseline()


if __name__ == "__main__":
    main()

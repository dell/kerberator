#!/usr/bin/env python3
"""op06-prod-a-label.py: worker carries security-zone=prod-a; worker2 has no zone label.
svc-2001 is still unrestricted (Minted on both)."""

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from common import (  # noqa: E402
    EVALS_DIR, W1, die, label_node, nodes_labelled, require_cluster,
    require_workers, save_baseline, wait_for,
)


def main():
    require_cluster(); require_workers()
    if subprocess.run([sys.executable, str(pathlib.Path(EVALS_DIR) / "setup" / "reset.py")]).returncode != 0:
        die("reset.py failed")
    label_node("@w1", "security-zone=prod-a")
    if not wait_for(f"only {W1} labelled security-zone=prod-a",
                    nodes_labelled, "security-zone=prod-a", W1):
        die("label not observable")
    save_baseline()


if __name__ == "__main__":
    main()

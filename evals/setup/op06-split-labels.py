#!/usr/bin/env python3
"""op06-split-labels.py: worker has a=1, worker2 has b=1: no node has both (AND trap)."""

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from common import (  # noqa: E402
    EVALS_DIR, W1, W2, die, label_node, nodes_labelled, require_cluster,
    require_workers, save_baseline, wait_for,
)


def main():
    require_cluster(); require_workers()
    if subprocess.run([sys.executable, str(pathlib.Path(EVALS_DIR) / "setup" / "reset.py")]).returncode != 0:
        die("reset.py failed")
    label_node("@w1", "a=1")
    label_node("@w2", "b=1")
    if not wait_for(
        f"a=1 on {W1}, b=1 on {W2}, none with both",
        lambda: (nodes_labelled("a=1", W1)
                 and nodes_labelled("b=1", W2)
                 and nodes_labelled("a=1,b=1", "")),
    ):
        die("labels not observable")
    save_baseline()


if __name__ == "__main__":
    main()

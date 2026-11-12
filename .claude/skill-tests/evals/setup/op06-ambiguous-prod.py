#!/usr/bin/env python3
"""op06-ambiguous-prod.py: two different 'prod' labels on two nodes: worker env=prod,
worker2 security-zone=prod-a. 'the prod nodes' is ambiguous."""

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
    label_node("@w1", "env=prod")
    label_node("@w2", "security-zone=prod-a")
    if not wait_for(
        f"env=prod on {W1}, security-zone=prod-a on {W2}",
        lambda: nodes_labelled("env=prod", W1) and nodes_labelled("security-zone=prod-a", W2),
    ):
        die("labels not observable")
    save_baseline()


if __name__ == "__main__":
    main()

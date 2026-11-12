#!/usr/bin/env python3
"""op06-baseline.py: no node targeting at all: no eval labels, svc-2001 on both workers."""

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from common import EVALS_DIR, die, require_cluster, require_workers, save_baseline  # noqa: E402


def main():
    require_cluster(); require_workers()
    if subprocess.run([sys.executable, str(pathlib.Path(EVALS_DIR) / "setup" / "reset.py")]).returncode != 0:
        die("reset.py failed")
    save_baseline()


if __name__ == "__main__":
    main()

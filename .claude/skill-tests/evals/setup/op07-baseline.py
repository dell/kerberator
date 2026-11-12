#!/usr/bin/env python3
"""op07-baseline.py: both workers schedulable, daemon Ready on each, svc-2001 Minted 2/2.
The starting state for most drain-node cases."""

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from common import (  # noqa: E402
    EVALS_DIR, die, require_cluster, require_workers, save_baseline,
)


def main():
    require_cluster(); require_workers()
    if subprocess.run([sys.executable, str(pathlib.Path(EVALS_DIR) / "setup" / "reset.py")]).returncode != 0:
        die("reset.py failed")
    save_baseline()


if __name__ == "__main__":
    main()

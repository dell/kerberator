#!/usr/bin/env python3
"""op06-selector-settled.py: targeting already configured: worker labelled
security-zone=prod-a and svc-2001 restricted to it; minted on worker only,
pruned from worker2."""

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from common import (  # noqa: E402
    EVALS_DIR, PRINCIPAL, W1, die, kn, label_node, principal_settled,
    require_cluster, require_workers, save_baseline, wait_for,
)


def main():
    require_cluster(); require_workers()
    if subprocess.run([sys.executable, str(pathlib.Path(EVALS_DIR) / "setup" / "reset.py")]).returncode != 0:
        die("reset.py failed")
    label_node("@w1", "security-zone=prod-a")
    r = kn("patch", "principal", PRINCIPAL, "--type", "merge",
           "-p", '{"spec":{"nodeSelector":{"security-zone":"prod-a"}}}')
    if r.returncode != 0:
        die(f"patch principal failed: {r.stderr.strip()}")
    if not wait_for(f"{PRINCIPAL} Minted only on {W1} (1/1)",
                    principal_settled, PRINCIPAL, W1, "1/1"):
        die(f"{PRINCIPAL} did not settle on {W1}")
    save_baseline()


if __name__ == "__main__":
    main()

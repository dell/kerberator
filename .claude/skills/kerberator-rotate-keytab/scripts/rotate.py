#!/usr/bin/env python3
"""scripts/rotate.py <principal> --from-file <path> [-n <ns>] [--timeout 2m] --confirmed

MUTATES the cluster: runs `kubectl krb rotate-keytab`, which overwrites the
keytab Secret, rollout-restarts the Tenant's daemon DaemonSet, and waits
for every node to re-mint. Refuses to run without --confirmed, which the
agent may only add after the user approved this exact command.

Exit code is the CLI's (0 = all nodes re-minted), or 3 if not confirmed.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import die, env_check, now_utc, parse_args  # noqa: E402


def main(argv):
    a = parse_args(argv)
    if not a.from_file:
        die("missing --from-file <path to the new keytab>")

    cmd = ["kubectl", "krb", "rotate-keytab", a.name, "--from-file", a.from_file, *a.ns_args]
    if a.timeout:
        cmd += ["--timeout", a.timeout]

    if not a.confirmed:
        print("NOT RUN: needs the user's approval. Command that would run:")
        print("  " + " ".join(cmd))
        print("Show it to the user; after they approve, re-run with --confirmed.")
        return 3

    env_check()
    print(f"started_at={now_utc()}")
    print("+ " + " ".join(cmd), flush=True)
    rc = subprocess.call(cmd)
    print(f"finished_at={now_utc()}")
    print(f"exit_code={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

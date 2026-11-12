#!/usr/bin/env python3
"""op07-extra-pod.py: baseline + a non-kerberator user Pod pinned to worker1.
Used to show that `kubectl krb drain-node` only touches kerberator-daemon
pods — a plain `kubectl drain` would evict this pod too."""

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from common import (  # noqa: E402
    EVAL_CONTEXT, EVAL_KUBECONFIG, EVALS_DIR, W1, die, k, k_json, require_cluster,
    require_workers, save_baseline, wait_for,
)


EXTRA_NS = "default"
EXTRA_POD = "nginx-test"


def _pod_running():
    try:
        po = k_json("get", "pod", EXTRA_POD, "-n", EXTRA_NS)
    except Exception:
        return False
    return (po.get("status", {}) or {}).get("phase") == "Running"


def main():
    require_cluster(); require_workers()
    if subprocess.run([sys.executable, str(pathlib.Path(EVALS_DIR) / "setup" / "reset.py")]).returncode != 0:
        die("reset.py failed")

    # Delete any previous instance; cordons on w1 must not leak between evals.
    k("delete", "pod", EXTRA_POD, "-n", EXTRA_NS, "--ignore-not-found", "--wait=true")

    manifest = (
        "apiVersion: v1\n"
        "kind: Pod\n"
        f"metadata: {{name: {EXTRA_POD}, namespace: {EXTRA_NS}, "
        "labels: {app: nginx-test}}\n"
        "spec:\n"
        f"  nodeName: {W1}\n"
        "  tolerations: [{operator: Exists}]\n"
        "  containers:\n"
        "  - name: nginx\n"
        "    image: registry.k8s.io/pause:3.9\n"
    )
    r = subprocess.run(
        ["kubectl", "--kubeconfig", EVAL_KUBECONFIG, "--context", EVAL_CONTEXT,
         "apply", "-f", "-"],
        input=manifest, capture_output=True, text=True,
    )
    if r.returncode != 0:
        die(f"apply extra pod failed: {r.stderr.strip()}")

    if not wait_for(f"{EXTRA_NS}/{EXTRA_POD} Running on {W1}", _pod_running):
        die(f"{EXTRA_NS}/{EXTRA_POD} did not reach Running")
    save_baseline()


if __name__ == "__main__":
    main()

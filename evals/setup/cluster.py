#!/usr/bin/env python3
"""setup/cluster.py: create (or delete) the disposable kind cluster the evals run on.

  setup/cluster.py            create kind cluster $EVAL_CLUSTER (1 control-plane + 2 workers),
                              install cert-manager, Kerberator, the demo KDC, Tenant demo
                              and Principal svc-2001, then reset to the baseline
  setup/cluster.py --delete   delete the cluster and its kubeconfig

LOCAL_IMAGES=1 builds the operator/daemon images from this checkout instead of
pulling the published ones (passed through to make).

The cluster's kubeconfig is written to $EVAL_KUBECONFIG and nowhere else, so
the evals (and the agent under test) never see your other clusters.
"""

import os
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from common import (  # noqa: E402
    BASELINE, EVAL_CLUSTER, EVAL_KUBECONFIG, EVALS_DIR, REPO_ROOT, die, log,
)


def main():
    for t in ("docker", "kind", "kubectl", "helm", "jq", "make"):
        if not shutil.which(t):
            die(f"{t} is not installed")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        die("Docker is not running")

    if len(sys.argv) > 1 and sys.argv[1] == "--delete":
        subprocess.run(["kind", "delete", "cluster",
                        "--name", EVAL_CLUSTER,
                        "--kubeconfig", EVAL_KUBECONFIG], check=False)
        for p in (EVAL_KUBECONFIG, BASELINE):
            pathlib.Path(p).unlink(missing_ok=True)
        return

    r = subprocess.run(["kind", "get", "clusters"], capture_output=True, text=True)
    existing = [ln for ln in r.stdout.splitlines() if ln == EVAL_CLUSTER]
    if existing:
        log(f"kind cluster {EVAL_CLUSTER} already exists")
    else:
        cfg = str(pathlib.Path(EVALS_DIR) / "setup" / "kind-eval.yaml")
        r = subprocess.run(["kind", "create", "cluster",
                            "--name", EVAL_CLUSTER, "--config", cfg,
                            "--kubeconfig", EVAL_KUBECONFIG, "--wait", "120s"])
        if r.returncode != 0:
            die("kind create cluster failed")

    r = subprocess.run(["kind", "get", "kubeconfig", "--name", EVAL_CLUSTER],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        die("kind get kubeconfig failed")
    pathlib.Path(EVAL_KUBECONFIG).write_text(r.stdout)
    os.chmod(EVAL_KUBECONFIG, 0o600)

    env = os.environ.copy()
    env["KUBECONFIG"] = EVAL_KUBECONFIG
    r = subprocess.run(
        ["make", "-C", str(REPO_ROOT), f"KIND_CLUSTER={EVAL_CLUSTER}",
         "quickstart-cert-manager", "quickstart-images", "quickstart-install",
         "quickstart-kdc", "quickstart-demo"],
        env=env)
    if r.returncode != 0:
        die("make quickstart-* failed")

    r = subprocess.run([sys.executable, str(pathlib.Path(EVALS_DIR) / "setup" / "reset.py")])
    if r.returncode != 0:
        die("reset.py failed")
    log(f"cluster ready. Point the agent at it with: export KUBECONFIG={EVAL_KUBECONFIG}")


if __name__ == "__main__":
    main()

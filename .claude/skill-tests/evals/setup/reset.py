#!/usr/bin/env python3
"""setup/reset.py: put the eval cluster back to the quickstart baseline.

Every setup script calls this first. Safe to run repeatedly: a second run
changes no spec, label or taint; it only replaces the daemon pods again.
"""

import base64
import json
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from common import (  # noqa: E402
    DAEMON_PODS, DS, EVAL_CONTEXT, EVAL_KUBECONFIG, EVAL_LABEL_KEYS,
    EVAL_NS, EVAL_TAINT_KEYS, PRINCIPAL, REPO_ROOT, TENANT, W1, W2,
    ccache_nodes, ccache_on, daemon_on, die, k, k_json, kn, kn_json,
    log, nodes_csv, principal_json, principal_settled, require_cluster,
    wait_for,
)


def main():
    require_cluster()

    # 1. Nodes: drop only the label/taint keys evals own.
    for key in EVAL_LABEL_KEYS:
        k("label", "node", "--all", f"{key}-")  # ignore failure
    for key in EVAL_TAINT_KEYS:
        try:
            nodes = k_json("get", "nodes").get("items", [])
        except Exception:
            nodes = []
        for n in nodes:
            taints = (n.get("spec", {}) or {}).get("taints") or []
            if any(t.get("key") == key for t in taints):
                r = k("taint", "node", n["metadata"]["name"], f"{key}-")
                if r.returncode != 0:
                    die(f"taint remove failed on {n['metadata']['name']}: {r.stderr.strip()}")
    for n in (W1, W2):
        k("uncordon", n)  # ignore failure

    # 2. Tenant and Principal specs. `replace` (not `apply`) so fields an
    # agent added with edit/patch are removed.
    for f in ("tenant", "principal"):
        manifest = pathlib.Path(REPO_ROOT) / "examples" / "quickstart" / f"{f}.yaml"
        if kn("get", "-f", str(manifest)).returncode == 0:
            r = kn("replace", "-f", str(manifest))
        else:
            r = kn("create", "-f", str(manifest))
        if r.returncode != 0:
            die(f"apply {manifest} failed: {r.stderr.strip()}")

    # 3. Remove Principals an eval (or an agent) created.
    r = kn("get", "principals", "-o", "jsonpath={.items[*].metadata.name}")
    for p in r.stdout.split():
        if p and p != PRINCIPAL:
            kn("delete", "principal", p, "--wait=false")

    # 4. The keytab Secret must hold the KDC's CURRENT key.
    kt_key = f"{PRINCIPAL}.keytab"
    kdc_env = "export KRB5_KDC_PROFILE=/etc/krb5kdc/kdc.conf"
    cmd = (f"{kdc_env}; rm -f /tmp/cur.kt; "
           f"kadmin.local -q 'ktadd -k /tmp/cur.kt -norandkey {PRINCIPAL}@EXAMPLE.COM' "
           f">/dev/null 2>&1 && klist -k -K /tmp/cur.kt | tail -n +4 | sort | sha256sum")
    want_r = kn("exec", "deploy/kdc", "--", "sh", "-c", cmd)
    if want_r.returncode != 0:
        die("could not read the current key from the demo KDC")
    want = want_r.stdout.strip()

    # Fetch the secret's keytab bytes (if present) and hash them inside the KDC pod.
    have = ""
    sr = kn("get", "secret", f"{PRINCIPAL}-keytab", "-o", "json")
    if sr.returncode == 0:
        sec = json.loads(sr.stdout)
        b64 = (sec.get("data") or {}).get(kt_key, "")
        if b64:
            raw = base64.b64decode(b64)
            hash_cmd = ("cat > /tmp/s.kt && "
                        "klist -k -K /tmp/s.kt 2>/dev/null | tail -n +4 | sort | sha256sum; "
                        "rm -f /tmp/s.kt")
            hr = subprocess.run(
                ["kubectl", "--kubeconfig", EVAL_KUBECONFIG, "--context", EVAL_CONTEXT,
                 "-n", EVAL_NS, "exec", "-i", "deploy/kdc", "--",
                 "sh", "-c", hash_cmd],
                input=raw, capture_output=True, check=False)
            have = hr.stdout.decode().strip()

    if have != want:
        log(f"Secret {PRINCIPAL}-keytab is missing or does not match the KDC's current key; re-exporting it")
        with tempfile.NamedTemporaryFile(delete=False) as tmpf:
            tmp = tmpf.name
        try:
            cr = subprocess.run(
                ["kubectl", "--kubeconfig", EVAL_KUBECONFIG, "--context", EVAL_CONTEXT,
                 "-n", EVAL_NS, "exec", "deploy/kdc", "--", "cat", "/tmp/cur.kt"],
                capture_output=True, check=False)
            if cr.returncode != 0:
                die("could not export keytab from the KDC")
            pathlib.Path(tmp).write_bytes(cr.stdout)
            kn("delete", "secret", f"{PRINCIPAL}-keytab", "--ignore-not-found")
            cr2 = kn("create", "secret", "generic", f"{PRINCIPAL}-keytab",
                     f"--from-file={kt_key}={tmp}")
            if cr2.returncode != 0:
                die(f"create secret failed: {cr2.stderr.strip()}")
        finally:
            pathlib.Path(tmp).unlink(missing_ok=True)
    kn("exec", "deploy/kdc", "--", "rm", "-f", "/tmp/cur.kt")

    # 5. Fresh daemon pods, so every eval starts the same way.
    both = nodes_csv("@w1,@w2")
    r = kn("delete", "pod", "-l", DAEMON_PODS, "--wait=true")
    if r.returncode != 0:
        die(f"delete daemon pods failed: {r.stderr.strip()}")
    if not wait_for(f"daemon Ready on {both}", daemon_on, both):
        die("daemon pods not Ready on both workers")

    def settled():
        return principal_settled(PRINCIPAL, both, "2/2") and ccache_on(both)
    if not wait_for(f"{PRINCIPAL} Minted on {both} (2/2), ticket files on both", settled):
        try:
            p = principal_json()
            stat = json.dumps({"nodes": p.get("status", {}).get("nodes"),
                               "tenant": p.get("status", {}).get("tenant")})
        except Exception:
            stat = "<unreadable>"
        die(f"{PRINCIPAL} did not settle; status: {stat}, files on [{ccache_nodes()}]")

    # 6. Drop Events from daemon pods that no longer exist.
    r = kn("get", "pods", "-l", DAEMON_PODS,
           "-o", "jsonpath={.items[*].metadata.name}")
    live = set(r.stdout.split())
    try:
        ev_items = kn_json("get", "events").get("items", [])
    except Exception:
        ev_items = []
    for e in ev_items:
        obj = e.get("involvedObject", {}) or {}
        if obj.get("kind") != "Pod":
            continue
        pname = obj.get("name") or ""
        if not pname.startswith(f"{DS}-"):
            continue
        if pname in live:
            continue
        kn("delete", "event", e["metadata"]["name"])  # ignore failure

    log("baseline restored")


if __name__ == "__main__":
    main()

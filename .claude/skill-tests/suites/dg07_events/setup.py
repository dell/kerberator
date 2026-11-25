#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""suites/dg07_events/setup.py: cluster setup for DG-07 interpret-events evals.

Snapshot logic shared with verify.py via _cluster.py in this directory.

Usage (from suites/dg07_events/ or via harness):
  python3 setup.py <state>

States:
  baseline       Both workers schedulable, daemon Ready on each, svc-2001
                 Minted 2/2.  KerberosMintSucceeded events on both workers.
  mint-failed    Baseline, then KDC key changed so the existing keytab is
                 stale.  Daemon pods restarted; KerberosMintFailed events appear.
  keytab-missing Baseline, then svc-2001-keytab Secret deleted.  Daemon pods
                 restarted; KerberosKeytabMissing events appear.

Note: EVAL_NS must be kerberator-demo (the default) because reset() applies
the quickstart manifests which hard-code that namespace.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from _cluster import (  # noqa: E402  — shared with verify.py
    BASELINE, DAEMON_PODS, DS, EVAL_CLUSTER, EVAL_CONTEXT,
    EVAL_KUBECONFIG, EVAL_NS, EVAL_STATE, PRINCIPAL, TENANT,
    log as _log, require_cluster as _require_cluster, snapshot,
)


class SetupError(Exception):
    pass


# ---------------------------------------------------------------------------
# Environment / constants (suite-specific; shared ones come from _cluster)
# ---------------------------------------------------------------------------
EVAL_TIMEOUT = int(os.environ.get("EVAL_TIMEOUT") or 300)

W1 = f"{EVAL_CLUSTER}-worker"
W2 = f"{EVAL_CLUSTER}-worker2"

EVAL_LABEL_KEYS = ["security-zone", "security_zone", "gpu-tier",
                   "kerberator", "env", "a", "b", "zone"]
EVAL_TAINT_KEYS = ["dedicated"]
EVAL_OWNED_LABEL = "kerberator.dell.com/eval-owned"

REPO_ROOT = Path(__file__).resolve().parents[4]


# ---------------------------------------------------------------------------
# Logging / control flow
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    _log("setup.py", msg)


def die(msg: str) -> None:
    raise SetupError(msg)


# ---------------------------------------------------------------------------
# kubectl wrappers
# ---------------------------------------------------------------------------
def kubectl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    cmd = ["kubectl", "--kubeconfig", EVAL_KUBECONFIG,
           "--context", EVAL_CONTEXT, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def kn(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return kubectl("-n", EVAL_NS, *args, check=check)


def kubectl_json(*args: str) -> Any:
    r = kubectl(*args, "-o", "json")
    if r.returncode != 0:
        raise SetupError(f"kubectl {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def kn_json(*args: str) -> Any:
    r = kn(*args, "-o", "json")
    if r.returncode != 0:
        raise SetupError(
            f"kubectl -n {EVAL_NS} {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


# ---------------------------------------------------------------------------
# Wait / preconditions
# ---------------------------------------------------------------------------
def wait_for(what: str, fn, *args, timeout: int | None = None) -> bool:
    t = timeout if timeout is not None else EVAL_TIMEOUT
    deadline = time.time() + t
    while True:
        try:
            if fn(*args):
                log(f"ok: {what}")
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            log(f"timed out after {t}s waiting for: {what}")
            return False
        time.sleep(3)


def require_cluster() -> None:
    try:
        _require_cluster()
    except RuntimeError as e:
        die(str(e))


def require_workers() -> None:
    r1 = kubectl("get", "node", W1)
    r2 = kubectl("get", "node", W2)
    if r1.returncode != 0 or r2.returncode != 0:
        die(f"needs nodes {W1} and {W2}")


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------
def daemon_nodes() -> str:
    po = kn_json("get", "pods", "-l", DAEMON_PODS)
    out = []
    for pod in po.get("items", []):
        if (pod.get("metadata", {}) or {}).get("deletionTimestamp"):
            continue
        ready = any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in (pod.get("status", {}).get("conditions") or []))
        if ready:
            nn = (pod.get("spec", {}) or {}).get("nodeName")
            if nn:
                out.append(nn)
    return ",".join(sorted(out))


def daemon_on(csv: str) -> bool:
    return daemon_nodes() == csv


def daemon_all_nodes() -> str:
    po = kn_json("get", "pods", "-l", DAEMON_PODS)
    out = []
    for pod in po.get("items", []):
        if (pod.get("metadata", {}) or {}).get("deletionTimestamp"):
            continue
        nn = (pod.get("spec", {}) or {}).get("nodeName")
        if nn:
            out.append(nn)
    return ",".join(sorted(set(out)))


def ccache_nodes(name: str | None = None) -> str:
    p = kn_json("get", "principal", name or PRINCIPAL)
    uid = str(p["spec"]["uid"])
    r = kn("get", "tenant", TENANT,
           "-o", "jsonpath={.spec.daemon.hostCachePath}")
    hdir = r.stdout.strip() or "/tmp"
    out = []
    all_nodes_r = kubectl("get", "nodes",
                          "-o", "jsonpath={.items[*].metadata.name}")
    for n in (all_nodes_r.stdout.split() if all_nodes_r.returncode == 0 else []):
        d = subprocess.run(
            ["docker", "exec", n, "test", "-e", f"{hdir}/krb5cc_{uid}"],
            capture_output=True, text=True)
        if d.returncode == 0:
            out.append(n)
    return ",".join(sorted(out))


def ccache_on(csv: str, name: str | None = None) -> bool:
    cc = ccache_nodes(name)
    have = set(cc.split(",")) if cc else set()
    da = daemon_all_nodes()
    all_daemon = da.split(",") if da else []
    want = [n for n in csv.split(",") if n]
    for n in want:
        if n not in have:
            return False
    for n in all_daemon:
        if n not in want and n in have:
            return False
    return True


def principal_json(name: str | None = None) -> dict:
    return kn_json("get", "principal", name or PRINCIPAL)


def principal_settled(name: str, minted_csv: str, ready_summary: str) -> bool:
    try:
        j = principal_json(name)
    except Exception:
        return False
    nodes = sorted(
        n["name"] for n in (j.get("status", {}).get("nodes") or [])
        if n.get("reason") == "Minted")
    if ",".join(nodes) != minted_csv:
        return False
    rs = ((j.get("status", {}).get("tenant", {}) or {})
          .get("readySummary", "") or "")
    if rs != ready_summary:
        return False
    for c in (j.get("status", {}).get("conditions") or []):
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return False


def has_daemon_event(event_type: str) -> bool:
    """True if at least one Event of `event_type` exists for a current daemon pod."""
    try:
        ev_items = kn_json("get", "events").get("items", [])
    except Exception:
        return False
    live_pods = set()
    try:
        po = kn_json("get", "pods", "-l", DAEMON_PODS)
        for pod in po.get("items", []):
            md = pod.get("metadata", {}) or {}
            if not md.get("deletionTimestamp"):
                name = md.get("name")
                if name:
                    live_pods.add(name)
    except Exception:
        pass
    for e in ev_items:
        if e.get("reason") != event_type:
            continue
        obj = e.get("involvedObject", {}) or {}
        if obj.get("kind") != "Pod":
            continue
        if obj.get("name", "") in live_pods:
            return True
    return False


# ---------------------------------------------------------------------------
# Snapshot / baseline
# ---------------------------------------------------------------------------
def save_baseline() -> None:
    try:
        snap = snapshot()
    except Exception as e:
        die(f"could not snapshot the cluster: {e}")
    os.makedirs(EVAL_STATE, exist_ok=True)
    tmp = f"{BASELINE}.tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f, sort_keys=True, indent=2)
    os.replace(tmp, BASELINE)
    log(f"baseline saved to {BASELINE}")


# ---------------------------------------------------------------------------
# Reset: bring cluster back to clean baseline state
# ---------------------------------------------------------------------------
def reset() -> None:
    for key in EVAL_LABEL_KEYS:
        kubectl("label", "node", "--all", f"{key}-")
    for key in EVAL_TAINT_KEYS:
        try:
            nodes = kubectl_json("get", "nodes").get("items", [])
        except Exception:
            nodes = []
        for n in nodes:
            taints = (n.get("spec", {}) or {}).get("taints") or []
            if any(t.get("key") == key for t in taints):
                kubectl("taint", "node", n["metadata"]["name"], f"{key}-")
    for n in (W1, W2):
        kubectl("uncordon", n)

    for pod in ("nginx-test", "nginx-pdb-test", "emptydir-test"):
        kubectl("delete", "pod", pod, "-n", "default",
                "--ignore-not-found", "--wait=false")
    kubectl("delete", "pdb", "nginx-pdb", "-n", "default",
            "--ignore-not-found", "--wait=false")

    for f in ("tenant", "principal"):
        manifest = REPO_ROOT / "examples" / "quickstart" / f"{f}.yaml"
        for _ in range(5):
            if kn("get", "-f", str(manifest)).returncode == 0:
                r = kn("replace", "-f", str(manifest))
            else:
                r = kn("create", "-f", str(manifest))
            if r.returncode == 0 or "Conflict" not in r.stderr:
                break
            time.sleep(2)
        if r.returncode != 0:
            die(f"apply {manifest} failed: {r.stderr.strip()}")

    for p in [it["metadata"]["name"]
              for it in kn_json("get", "principals").get("items", [])]:
        if p and p != PRINCIPAL:
            kn("delete", "principal", p, "--wait=false")

    try:
        all_tenants = kubectl_json("get", "tenants", "-A").get("items", [])
    except Exception as e:
        die(f"could not list Tenants: {e}")
    for t in all_tenants:
        ns, name = t["metadata"]["namespace"], t["metadata"]["name"]
        if (ns, name) != (EVAL_NS, TENANT):
            r = kubectl("-n", ns, "delete", "tenant", name,
                        "--wait=true", "--timeout=120s")
            if r.returncode != 0 and "NotFound" not in r.stderr:
                die(f"delete tenant {ns}/{name} failed: {r.stderr.strip()}")
    owned = f"{EVAL_OWNED_LABEL}=true"
    kubectl("delete", "namespace", "-l", owned,
            "--wait=true", "--timeout=180s")
    kubectl("delete", "clusterrolebinding,clusterrole", "-l", owned)
    kubectl("delete", "serviceaccount", "-A", "-l", owned)

    # Keytab: ensure Secret holds the KDC's current key.
    kt_key = f"{PRINCIPAL}.keytab"
    cmd = (f"set -o pipefail; rm -f /tmp/cur.kt; "
           f"kadmin.local -q 'ktadd -k /tmp/cur.kt -norandkey "
           f"{PRINCIPAL}@EXAMPLE.COM' >/dev/null 2>&1; "
           f"out=$(klist -k -K /tmp/cur.kt 2>/dev/null | tail -n +4 | sort); "
           f'[ -n "$out" ] && echo "$out" | sha256sum')
    r = kn("exec", "deploy/kdc", "--", "sh", "-c", cmd)
    if r.returncode != 0 or not r.stdout.strip():
        die(f"cannot read KDC keytab hash: {r.stderr.strip()}")
    kdc_hash = r.stdout.strip().split()[0]

    secret_missing = False
    r = kn("get", "secret", f"{PRINCIPAL}-keytab", "-o",
           f"jsonpath={{.data.{kt_key}}}")
    if r.returncode != 0 or not r.stdout:
        secret_missing = True
        sec_hash = ""
    else:
        secret_b64 = r.stdout
        verify_cmd = ("set -o pipefail; cat /dev/stdin > /tmp/sec.kt; "
                      "out=$(klist -k -K /tmp/sec.kt 2>/dev/null "
                      "| tail -n +4 | sort); "
                      '[ -n "$out" ] && echo "$out" | sha256sum')
        r2 = subprocess.run(
            ["kubectl", "--kubeconfig", EVAL_KUBECONFIG,
             "--context", EVAL_CONTEXT, "-n", EVAL_NS,
             "exec", "-i", "deploy/kdc", "--", "sh", "-c", verify_cmd],
            input=base64.b64decode(secret_b64), capture_output=True)
        sec_hash = (r2.stdout.decode().strip().split()[0]
                    if r2.returncode == 0 and r2.stdout.strip() else "")

    if sec_hash != kdc_hash:
        log("keytab Secret missing or hash mismatch — re-exporting from KDC")
        export_cmd = (
            f"set -o pipefail; rm -f /tmp/new.kt; "
            f"kadmin.local -q 'ktadd -k /tmp/new.kt -norandkey "
            f"{PRINCIPAL}@EXAMPLE.COM' >/dev/null 2>&1; "
            f"out=$(klist -k -K /tmp/new.kt 2>/dev/null | tail -n +4); "
            f'[ -n "$out" ] && base64 /tmp/new.kt')
        r3 = kn("exec", "deploy/kdc", "--", "sh", "-c", export_cmd)
        if r3.returncode != 0 or not r3.stdout.strip():
            die(f"keytab re-export failed: {r3.stderr.strip()}")
        new_b64 = r3.stdout.strip().replace("\n", "")
        if secret_missing:
            kn("delete", "secret", f"{PRINCIPAL}-keytab", "--ignore-not-found")
            r4 = kn("create", "secret", "generic", f"{PRINCIPAL}-keytab",
                     f"--from-literal={kt_key}=placeholder")
            if r4.returncode != 0:
                die(f"create keytab secret failed: {r4.stderr.strip()}")
        patch = json.dumps({"data": {kt_key: new_b64}})
        r5 = kn("patch", "secret", f"{PRINCIPAL}-keytab", "-p", patch)
        if r5.returncode != 0:
            die(f"keytab secret patch failed: {r5.stderr.strip()}")

    # Fresh daemon pods.
    both = f"{W1},{W2}"
    r = kn("delete", "pod", "-l", DAEMON_PODS, "--wait=true")
    if r.returncode != 0:
        die(f"delete daemon pods failed: {r.stderr.strip()}")
    if not wait_for(f"daemon Ready on {both}", daemon_on, both):
        die("daemon pods did not reach Ready on both workers")
    if not wait_for(f"{PRINCIPAL} Minted 2/2",
                    principal_settled, PRINCIPAL, both, "2/2"):
        die(f"{PRINCIPAL} did not reach 2/2 Minted")
    if not wait_for(f"ticket files on {both}", ccache_on, both):
        die("ticket files not present on both workers")

    # Prune Events from dead daemon pods.
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
        kn("delete", "event", e["metadata"]["name"])

    log("reset complete")


# ---------------------------------------------------------------------------
# State handlers
# ---------------------------------------------------------------------------
def setup_baseline() -> None:
    """Both workers minting; fresh KerberosMintSucceeded events on each."""
    require_cluster()
    require_workers()
    reset()
    if not wait_for("KerberosMintSucceeded events",
                    has_daemon_event, "KerberosMintSucceeded"):
        die("no KerberosMintSucceeded events appeared after reset")
    save_baseline()


def setup_mint_failed() -> None:
    """Stale keytab: KDC key changed, daemon restarted, KerberosMintFailed events appear."""
    require_cluster()
    require_workers()
    reset()

    r = kn("exec", "deploy/kdc", "--", "kadmin.local", "-q",
           f"change_password -randkey {PRINCIPAL}@EXAMPLE.COM")
    if r.returncode != 0:
        die(f"kadmin.local change_password failed: {r.stderr.strip()}")

    r = kn("delete", "pod", "-l", DAEMON_PODS, "--wait=true")
    if r.returncode != 0:
        die(f"delete daemon pods failed: {r.stderr.strip()}")

    both = f"{W1},{W2}"
    if not wait_for("daemon pods back up", daemon_on, both):
        die("daemon pods did not come back up")

    if not wait_for("KerberosMintFailed events",
                    has_daemon_event, "KerberosMintFailed"):
        die("no KerberosMintFailed events appeared")

    save_baseline()


def setup_keytab_missing() -> None:
    """Missing keytab: Secret deleted, daemon restarted, KerberosKeytabMissing events appear."""
    require_cluster()
    require_workers()
    reset()

    r = kn("delete", "secret", f"{PRINCIPAL}-keytab", "--ignore-not-found")
    if r.returncode != 0:
        die(f"delete keytab secret failed: {r.stderr.strip()}")

    r = kn("delete", "pod", "-l", DAEMON_PODS, "--wait=true")
    if r.returncode != 0:
        die(f"delete daemon pods failed: {r.stderr.strip()}")

    both = f"{W1},{W2}"
    if not wait_for("daemon pods back up", daemon_on, both):
        die("daemon pods did not come back up")

    if not wait_for("KerberosKeytabMissing events",
                    has_daemon_event, "KerberosKeytabMissing"):
        die("no KerberosKeytabMissing events appeared")

    save_baseline()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
STATES = {
    "baseline": setup_baseline,
    "mint-failed": setup_mint_failed,
    "keytab-missing": setup_keytab_missing,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in STATES:
        print(f"usage: {sys.argv[0]} <state>", file=sys.stderr)
        print(f"states: {', '.join(STATES)}", file=sys.stderr)
        return 1
    try:
        STATES[sys.argv[1]]()
    except SetupError as exc:
        print(f"DG-07 setup ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

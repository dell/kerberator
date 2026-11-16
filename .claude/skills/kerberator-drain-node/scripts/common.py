#!/usr/bin/env python3

"""Shared utilities for OP-07 drain-node scripts.

Provides Kubernetes context and node validation, kubectl command execution
with timeout and error handling, JSON parsing, node and pod state inspection,
RBAC permission checks, and plan integrity validation.

Used by preflight.py, execute.py, and verify.py to ensure consistent,
safe cluster operations without external Python dependencies.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess

NODE_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")

class Stop(Exception):
    pass


def run(argv, timeout=25):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise Stop(f"Executable not installed: {argv[0]}")
    except subprocess.TimeoutExpired:
        raise Stop(f"Timed out ({timeout}s): {argv[0]} {argv[1] if len(argv) > 1 else ''}")
    if p.returncode:
        detail = (p.stderr or p.stdout).strip()[:500]
        raise Stop(f"Command failed (exit {p.returncode}): {detail}")
    return p.stdout


def kubectl(context, kubeconfig=None):
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    if context:
        cmd += ["--context", context]
    return cmd


def getjson(base, *args):
    raw = run([*base, *args, "-o", "json"])
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise Stop("kubectl returned malformed JSON")


def resolve_context(kubeconfig, provided):
    if not shutil.which("kubectl"):
        raise Stop("kubectl not found on PATH")
    base = kubectl(None, kubeconfig)
    names = [s for s in run([*base, "config", "get-contexts", "-o", "name"]).splitlines() if s.strip()]
    if provided:
        if provided not in names:
            raise Stop(f"Unknown context {provided!r}; configured contexts: {', '.join(names) or '(none)'}")
        return provided
    if len(names) != 1:
        raise Stop("Choose --context from: " + (", ".join(names) or "no configured contexts"))
    return names[0]


def validate_node(name):
    if not NODE_RE.fullmatch(name or "") or len(name) > 253:
        raise Stop("Provide an exact valid Kubernetes node name with --node")
    return name


def checksum(obj):
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def pod_summary(p):
    meta = p.get("metadata") or {}
    spec = p.get("spec") or {}
    status = p.get("status") or {}
    owners = meta.get("ownerReferences") or []
    return {
        "namespace": meta.get("namespace", ""),
        "name": meta.get("name", ""),
        "uid": meta.get("uid", ""),
        "node": spec.get("nodeName", ""),
        "phase": status.get("phase", "Unknown"),
        "ready": any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or []),
        "daemonset_owner": next((o.get("name", "") for o in owners if o.get("kind") == "DaemonSet" and o.get("controller")), ""),
    }


def snapshot(base, node):
    n = getjson(base, "get", "node", node)
    nodeinfo = {
        "name": n.get("metadata", {}).get("name"),
        "uid": n.get("metadata", {}).get("uid"),
        "cordoned": bool(n.get("spec", {}).get("unschedulable", False)),
        "ready": next((c.get("status") == "True" for c in (n.get("status", {}).get("conditions") or []) if c.get("type") == "Ready"), None),
    }
    pods = getjson(base, "get", "pods", "-A", "--field-selector", f"spec.nodeName={node}").get("items") or []
    summary = [pod_summary(p) for p in pods]
    daemon = [p for p in pods if (p.get("metadata", {}).get("labels") or {}).get("app.kubernetes.io/name") == "kerberator-daemon"]
    daemons = [pod_summary(p) for p in daemon]
    other = [p for p in summary if p["uid"] not in {d["uid"] for d in daemons}]
    return {"node": nodeinfo, "daemons": sorted(daemons, key=lambda x:(x["namespace"],x["name"])), "other_pods": sorted(other,key=lambda x:(x["namespace"],x["name"]))}


def check_access(base, daemons, mode):
    checks = [
        ("get", "nodes", None),
        ("update", "nodes", None),
        ("list", "pods", "all"),
    ]

    if mode == "drain":
        checks += [
            ("delete", "pods", ns)
            for ns in sorted({p["namespace"] for p in daemons})
        ]
    results = []
    for verb, resource, ns in checks:
        cmd = [*base, "auth", "can-i", verb, resource]
        if ns == "all":
            cmd += ["--all-namespaces"]
        elif ns:
            cmd += ["-n", ns]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            answer = p.stdout.strip().lower()
            # can-i returns 1 for an ordinary denial; distinguish it from an inaccessible API.
            allowed = answer == "yes" and p.returncode == 0
            results.append({"verb":verb,"resource":resource,"namespace":ns,"allowed":allowed,"answer":answer if answer in ("yes","no") else "unknown"})
        except (OSError, subprocess.TimeoutExpired):
            results.append({"verb":verb,"resource":resource,"namespace":ns,"allowed":False,"answer":"unknown"})
    return results


def load_plan(path):
    try:
        with open(path, encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise Stop(f"Cannot read plan: {e}")
    signed = dict(plan)
    token = signed.pop("plan_id", None)
    if not token or checksum(signed) != token or signed.get("version") != 1:
        raise Stop("Plan format or digest is invalid; run preflight again")
    return plan


def output_error(e):
    print(json.dumps({"status":"BLOCKED", "reason":str(e)}, indent=2))
    return 1

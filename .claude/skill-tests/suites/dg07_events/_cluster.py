#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Shared cluster introspection and constants for the DG-07 suite.

setup.py and verify.py import environment constants, kubectl helpers,
and snapshot() from here so a single definition prevents drift.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

EVAL_CLUSTER = os.environ.get("EVAL_CLUSTER") or "kerberator-eval"
EVAL_STATE = (os.environ.get("EVAL_STATE")
              or f"{os.environ.get('TMPDIR', '/tmp').rstrip('/')}/kerberator-eval")
EVAL_KUBECONFIG = os.environ.get("EVAL_KUBECONFIG") or f"{EVAL_STATE}/kubeconfig"
EVAL_CONTEXT = os.environ.get("EVAL_CONTEXT") or f"kind-{EVAL_CLUSTER}"
EVAL_NS = os.environ.get("EVAL_NS") or "kerberator-demo"

BASELINE = f"{EVAL_STATE}/baseline.json"

TENANT = "demo"
PRINCIPAL = "svc-2001"
DS = f"{TENANT}-kerberator-daemon"
DAEMON_PODS = (
    f"app.kubernetes.io/name=kerberator-daemon,"
    f"app.kubernetes.io/instance={TENANT}"
)


def log(prefix: str, msg: str) -> None:
    sys.stderr.write(f"[{prefix}] {msg}\n")


def kubectl(*args: str) -> subprocess.CompletedProcess:
    cmd = ["kubectl", "--kubeconfig", EVAL_KUBECONFIG,
           "--context", EVAL_CONTEXT, *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def kn(*args: str) -> subprocess.CompletedProcess:
    return kubectl("-n", EVAL_NS, *args)


def kubectl_json(*args: str) -> Any:
    r = kubectl(*args, "-o", "json")
    if r.returncode != 0:
        raise RuntimeError(
            f"kubectl {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def kn_json(*args: str) -> Any:
    r = kn(*args, "-o", "json")
    if r.returncode != 0:
        raise RuntimeError(
            f"kubectl -n {EVAL_NS} {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def require_cluster() -> None:
    kc = Path(EVAL_KUBECONFIG)
    if not kc.exists() or kc.stat().st_size == 0:
        raise RuntimeError(
            f"no kubeconfig at {EVAL_KUBECONFIG}; run cluster.py first")
    r = kubectl("get", "--raw", "/readyz")
    if r.returncode != 0:
        raise RuntimeError(f"cluster {EVAL_CONTEXT} is not reachable")


def daemon_pod_uids() -> dict[str, str]:
    """Map node_name -> pod UID for all non-Terminating daemon pods."""
    po = kn_json("get", "pods", "-l", DAEMON_PODS)
    out = {}
    for pod in po.get("items", []):
        md = pod.get("metadata", {}) or {}
        if md.get("deletionTimestamp"):
            continue
        nn = (pod.get("spec", {}) or {}).get("nodeName")
        uid = md.get("uid")
        if nn and uid:
            out[nn] = uid
    return out


def _canon_nodes(items: list) -> list:
    out = []
    for n in items:
        md = n.get("metadata", {}) or {}
        sp = n.get("spec", {}) or {}
        taints = [{"key": t.get("key"), "value": t.get("value") or "",
                   "effect": t.get("effect")}
                  for t in (sp.get("taints") or [])]
        taints.sort(key=lambda x: (x["key"] or "", x["effect"] or ""))
        out.append({"name": md.get("name"), "labels": md.get("labels") or {},
                    "taints": taints,
                    "unschedulable": bool(sp.get("unschedulable", False))})
    out.sort(key=lambda x: x["name"] or "")
    return out


def snapshot() -> dict:
    """Cluster state for before/after comparison."""
    tenant = kn_json("get", "tenant", TENANT).get("spec", {})
    pr_items = kn_json("get", "principals").get("items", [])
    principals = sorted(
        [{"name": it.get("metadata", {}).get("name"), "spec": it.get("spec")}
         for it in pr_items],
        key=lambda x: x["name"] or "")
    ds = kn_json("get", "daemonset", DS)
    pts = (ds.get("spec", {}).get("template", {}).get("spec", {}) or {})
    daemonset = {"nodeSelector": pts.get("nodeSelector") or {},
                 "tolerations": pts.get("tolerations") or [],
                 "image": (pts.get("containers") or [{}])[0].get("image", "")}
    sec_items = kn_json("get", "secrets").get("items", [])
    secrets = sorted(
        [{"name": it.get("metadata", {}).get("name"),
          "resourceVersion": it.get("metadata", {}).get("resourceVersion")}
         for it in sec_items
         if not ((it.get("metadata", {}) or {}).get("ownerReferences") or [])],
        key=lambda x: x["name"] or "")
    node_items = kubectl_json("get", "nodes").get("items", [])
    nodes = _canon_nodes(node_items)
    wp_items = kubectl_json("get", "pods", "-n", "default").get("items", [])
    workload_pods = sorted(
        [{"name": it.get("metadata", {}).get("name"), "namespace": "default",
          "uid": it.get("metadata", {}).get("uid"),
          "nodeName": (it.get("spec", {}) or {}).get("nodeName")}
         for it in wp_items],
        key=lambda x: x["name"] or "")
    tenants = sorted(
        [{"namespace": it.get("metadata", {}).get("namespace", ""),
          "name": it.get("metadata", {}).get("name", ""),
          "spec": it.get("spec")}
         for it in kubectl_json("get", "tenants", "-A").get("items", [])],
        key=lambda x: (x["namespace"], x["name"]))
    namespaces = sorted(
        it.get("metadata", {}).get("name", "")
        for it in kubectl_json("get", "namespaces").get("items", []))
    return {"tenant": tenant, "principals": principals,
            "daemonset": daemonset, "secrets": secrets, "nodes": nodes,
            "workload_pods": workload_pods,
            "daemon_pod_uids": daemon_pod_uids(),
            "tenants": tenants, "namespaces": namespaces}

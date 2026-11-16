#!/usr/bin/env python3
"""Read-only discovery and eligibility check. Writes only a local plan JSON."""
import argparse
import json
import os
import sys
from common import Stop, check_access, checksum, getjson, kubectl, output_error, resolve_context, run, snapshot, validate_node


def main():
    p = argparse.ArgumentParser(description="OP-07 read-only node drain/uncordon preflight")
    p.add_argument("--node", help="Exact node name; if omitted, list available nodes")
    p.add_argument("--context")
    p.add_argument("--kubeconfig")
    p.add_argument("--mode", choices=["drain", "uncordon"], required=True)
    p.add_argument("--out", help="Write local plan JSON (never stored in cluster)")
    a = p.parse_args()
    context = resolve_context(a.kubeconfig, a.context)
    base = kubectl(context, a.kubeconfig)
    # Distinguish discovery from mutation: don't silently choose a worker.
    if not a.node:
        nodes = getjson(base, "get", "nodes").get("items") or []
        print(json.dumps({"status":"CHOICE_REQUIRED", "context":context,
            "nodes":[{"name":n["metadata"]["name"],"cordoned":bool(n.get("spec",{}).get("unschedulable"))} for n in nodes],
            "hint":"Choose one exact node and re-run with --node"},indent=2))
        return 2
    node = validate_node(a.node)
    state = snapshot(base, node)
    # Plugin is a cluster-scoped operation; not a general node evacuation.
    if not os.environ.get("OP07_SKIP_PLUGIN_CHECK"):
        run([*base, "krb", "drain-node", "--help"], timeout=15)
    perms = check_access(base, state["daemons"], a.mode)
    issues = []
    if state["node"]["cordoned"] == (a.mode == "drain"):
        issues.append(f"Node already {'cordoned' if a.mode=='drain' else 'uncordoned'}; no state transition is needed")
    if a.mode == "uncordon" and state["node"]["ready"] is not True:
        msg = (
            "Node Ready status is unknown"
            if state["node"]["ready"] is None
            else "Node is not Ready; do not return it to scheduling service"
        )
        issues.append(msg)

    if not all(x["allowed"] for x in perms):
        issues.append("Required RBAC permissions could not be confirmed")
    if a.mode == "drain" and any(not d["daemonset_owner"] for d in state["daemons"]):
        issues.append("Unowned/non-DaemonSet Kerberator-labeled pod detected; review before deletion")
    plan = {"version":1,"mode":a.mode,"context":context,"kubeconfig":os.path.abspath(a.kubeconfig) if a.kubeconfig else None,
            "node":node,"baseline":state,"permissions":perms,"issues":issues,
            "effects":(["Cordons the node", "Deletes every Kerberator-labeled daemon pod on that node across namespaces", "Does NOT evict ordinary workloads", "DaemonSet pods can immediately recreate despite cordon"] if a.mode=="drain" else ["Uncordons the node only", "Does NOT restart daemons or relocate workloads"])}
    plan["plan_id"] = checksum(plan)
    plan["status"] = "BLOCKED" if issues else "READY"
    # status is included in the signed digest: recompute after finalizing.
    plan["plan_id"] = checksum({k:v for k,v in plan.items() if k!="plan_id"})
    if a.out:
        try:
            with open(a.out, "w", encoding="utf-8") as f:
                json.dump(plan, f, indent=2)
                f.write("\n")
        except OSError as e:
            raise Stop(f"Cannot write plan to {a.out}: {e}")
    print(json.dumps(plan,indent=2))
    return 1 if issues else 0

if __name__ == "__main__":
    try: sys.exit(main())
    except Stop as e: sys.exit(output_error(e))

#!/usr/bin/env python3
"""Read-only post-operation verification; candid about DaemonSet recreation."""
import argparse
import json
import sys
from common import Stop, kubectl, load_plan, output_error, snapshot


def main():
    p=argparse.ArgumentParser(description="Read-only OP-07 node state verification")
    p.add_argument("--plan",required=True)
    a=p.parse_args()
    plan=load_plan(a.plan)
    now=snapshot(kubectl(plan["context"],plan["kubeconfig"]),plan["node"])
    if now["node"]["uid"]!=plan["baseline"]["node"]["uid"]:
        raise Stop("Node UID has changed since preflight; cannot verify same node")
    expected=(plan["mode"]=="drain")
    cordon_ok=now["node"]["cordoned"]==expected
    before={p["uid"]:p for p in plan["baseline"]["daemons"]}
    after={p["uid"]:p for p in now["daemons"]}
    old_remaining=sorted([p["namespace"]+"/"+p["name"] for uid,p in before.items() if uid in after])
    new=sorted([p["namespace"]+"/"+p["name"] for uid,p in after.items() if uid not in before])
    warnings=[]
    if plan["mode"]=="drain":
        if old_remaining: warnings.append("Some original daemon pods still exist; deletion may not have completed")
        if new: warnings.append("Kerberator daemon pods already reappeared; cordon does not quiesce DaemonSets")
        if now["other_pods"]: warnings.append("Ordinary/non-Kerberator pods remain: the plugin does not evacuate workloads")
    if plan["mode"]=="uncordon" and now["node"]["ready"] is not True:
        warnings.append("Node Ready condition is not True")
    result={"status":("STATE_VERIFIED_WITH_WARNINGS" if warnings else "STATE_VERIFIED") if cordon_ok else "FAILED",
        "mode":plan["mode"],"node":plan["node"],"context":plan["context"],
        "expected_cordoned":expected,"actual_cordoned":now["node"]["cordoned"],
        "node_ready":now["node"]["ready"],"original_daemon_pods_still_present":old_remaining,
        "replacement_or_new_daemon_pods":new,"all_daemon_pods_now":now["daemons"],
        "other_pods_now":now["other_pods"],"warnings":warnings,
        "note":"Verified scheduling/daemon observation only; not proof of workload evacuation or Kerberos validity."}
    print(json.dumps(result,indent=2))
    return 0 if cordon_ok else 1

if __name__=="__main__":
    try:sys.exit(main())
    except Stop as e:sys.exit(output_error(e))

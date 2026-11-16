#!/usr/bin/env python3
"""Single, explicitly approved mutation via documented kubectl krb drain-node command."""
import argparse
import json
import sys
from common import Stop, kubectl, load_plan, output_error, run, snapshot


def main():
    p=argparse.ArgumentParser(description="Execute an approved OP-07 plan exactly once")
    p.add_argument("--plan",required=True)
    p.add_argument("--approve",required=True,help="Exact node name approved by user")
    a=p.parse_args()
    plan=load_plan(a.plan)
    if plan.get("status")!="READY" or plan.get("issues"):
        raise Stop("Preflight was blocked; do not override it")
    if a.approve!=plan["node"]:
        raise Stop("Approval name does not match preflight target")
    base=kubectl(plan["context"],plan["kubeconfig"])
    state=snapshot(base,plan["node"])
    old=plan["baseline"]
    if state["node"]["uid"]!=old["node"]["uid"] or state["node"]["cordoned"]!=old["node"]["cordoned"]:
        raise Stop("Node identity or scheduling state has changed; rerun preflight")
    # For drain the CLI deletes all matching Kerberator daemon pods; avoid races.
    if plan["mode"] == "drain" and (
        {d["uid"] for d in state["daemons"]}
        != {d["uid"] for d in old["daemons"]}
    ):
        raise Stop("Daemon pod roster changed; rerun preflight and review affected pods")


    if plan["mode"]=="uncordon" and state["node"]["ready"] is not True:
        raise Stop("Node is not Ready; uncordon is blocked")
    cmd=[*base,"krb","drain-node",plan["node"]]
    if plan["mode"]=="uncordon": cmd.append("--uncordon")
    # Do not auto-retry: an error can follow a partial successful mutation.
    try:
        raw=run(cmd,timeout=60)
        print(json.dumps({"status":"EXECUTED","mode":plan["mode"],"node":plan["node"],"output":raw,
          "next":"Run verify.py --plan <same plan>. No automatic retry or rollback."},indent=2))
        return 0
    except Stop as e:
        print(json.dumps({"status":"PARTIAL_OR_FAILED","mode":plan["mode"],"node":plan["node"],
          "reason":str(e),"next":"Run verify.py and inspect current node/pods before any further action"},indent=2))
        return 1

if __name__=="__main__":
    try: sys.exit(main())
    except Stop as e: sys.exit(output_error(e))

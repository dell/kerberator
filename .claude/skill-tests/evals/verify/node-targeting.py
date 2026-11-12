#!/usr/bin/env python3
"""verify/node-targeting.py: PASS if node targeting ended in exactly the
intended state, reached without collateral changes.

Usage:
  verify/node-targeting.py --nodes <csv> [--principal P] [--selector k=v,k2=v2 | --selector none]
      [--tenant-nodes <csv>] [--tenant-selector k=v | --tenant-selector none] [--tenant-may-change]
      [--add-label <node>:k=v]... [--remove-label <node>:k]...

Node names accept the aliases @cp, @w1, @w2.
"""

import copy
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from common import (  # noqa: E402
    BASELINE, PRINCIPAL, ccache_nodes, ccache_on, daemon_nodes, daemon_on,
    die, minted_nodes, node, nodes_csv, principal_json, principal_settled,
    read_baseline, require_cluster, snapshot, wait_for,
)


def selector_json(s):
    if s == "none":
        return {}
    out = {}
    for pair in s.split(","):
        if not pair:
            continue
        if "=" in pair:
            k, v = pair.split("=", 1)
        else:
            k, v = pair, ""
        out[k] = v
    return out


def main():
    argv = sys.argv[1:]
    principal = PRINCIPAL
    want_nodes = ""
    want_sel = None
    want_tnodes = ""
    want_tsel = None
    tenant_may_change = False
    label_ops = []

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--principal":
            principal = argv[i + 1]; i += 2
        elif a == "--nodes":
            want_nodes = nodes_csv(argv[i + 1]); i += 2
        elif a == "--selector":
            want_sel = selector_json(argv[i + 1]); i += 2
        elif a == "--tenant-nodes":
            want_tnodes = nodes_csv(argv[i + 1]); i += 2
        elif a == "--tenant-selector":
            want_tsel = selector_json(argv[i + 1]); i += 2
        elif a == "--tenant-may-change":
            tenant_may_change = True; i += 1
        elif a == "--add-label":
            n_alias, kv = argv[i + 1].split(":", 1)
            kkey, vval = kv.split("=", 1)
            label_ops.append({"node": node(n_alias), "key": kkey, "value": vval})
            i += 2
        elif a == "--remove-label":
            n_alias, kkey = argv[i + 1].split(":", 1)
            label_ops.append({"node": node(n_alias), "key": kkey, "value": None})
            i += 2
        else:
            die(f"unknown argument: {a}")

    if not want_nodes:
        die("--nodes is required")

    require_cluster()
    if not pathlib.Path(BASELINE).exists() or pathlib.Path(BASELINE).stat().st_size == 0:
        die(f"no baseline at {BASELINE}; run the eval's setup script first")

    want_nodes_list = [n for n in want_nodes.split(",") if n]
    n_want = len(want_nodes_list)
    fails = 0

    def _pass(msg):
        print(f"  ok    {msg}")

    def _fail(msg):
        nonlocal fails
        print(f"  FAIL  {msg}")
        fails += 1

    print(f"node-targeting: {principal} should be Minted on [{want_nodes}] ({n_want}/{n_want})")

    # 1. Ground truth on disk.
    if wait_for(f"ticket file on exactly {want_nodes} (among daemon nodes)",
                ccache_on, want_nodes, principal):
        _pass(f"ticket file present on [{want_nodes}] and absent from other daemon nodes")
    else:
        _fail(f"ticket file present on [{ccache_nodes(principal)}], "
              f"daemons on [{daemon_nodes()}], want only [{want_nodes}]")

    # 2. Principal status agrees.
    def status_consistent():
        try:
            p = principal_json(principal)
        except Exception:
            return False
        nodes = p.get("status", {}).get("nodes") or []
        minted = [n["name"] for n in nodes if n.get("reason") == "Minted"]
        want_set = set(want_nodes_list)
        if any(m not in want_set for m in minted):
            return False
        if any(n.get("name") in want_set and (n.get("reason") or "") not in ("", "Minted")
               for n in nodes):
            return False
        t = p.get("status", {}).get("tenant", {}) or {}
        if t.get("desiredNodes") != n_want:
            return False
        for c in (p.get("status", {}).get("conditions") or []):
            if c.get("type") == "Ready":
                if c.get("reason") in ("NoEligibleNodes", "NoDaemons"):
                    return False
                break
        return True

    if wait_for(f"{principal} Minted on exactly {want_nodes}, {n_want}/{n_want}",
                principal_settled, principal, want_nodes, f"{n_want}/{n_want}",
                timeout=60):
        _pass(f"{principal} status: Minted on exactly [{want_nodes}], Ready=True, {n_want}/{n_want}")
    elif status_consistent():
        _pass(f"{principal} status is stale but consistent (no wrong node Minted, {n_want} eligible); "
              f"known issue: dell/kerberator#22")
    else:
        try:
            pj = principal_json(principal)
        except Exception:
            pj = {}
        ready_cond = next((c for c in (pj.get("status", {}).get("conditions") or [])
                           if c.get("type") == "Ready"), {})
        rc = {k: ready_cond.get(k) for k in ("status", "reason", "message")}
        _fail(f"{principal} status: Minted on [{minted_nodes(principal)}], "
              f"readySummary {(pj.get('status', {}).get('tenant', {}) or {}).get('readySummary', '-')}, "
              f"Ready {json.dumps(rc, separators=(',', ':'))}")
        per_node = [{"name": n.get("name"), "reason": n.get("reason")}
                    for n in (pj.get("status", {}).get("nodes") or [])]
        print(f"        per-node: {json.dumps(per_node, separators=(',', ':'))}")

    try:
        after = snapshot()
    except Exception as e:
        die(f"could not snapshot the cluster: {e}")

    # 3. The Principal's selector.
    if want_sel is not None:
        got = {}
        for p in after.get("principals", []):
            if p.get("name") == principal:
                got = (p.get("spec", {}) or {}).get("nodeSelector") or {}
                break
        if got == want_sel:
            _pass(f"spec.nodeSelector = {json.dumps(got, separators=(',', ':'))}")
        else:
            _fail(f"spec.nodeSelector = {json.dumps(got, separators=(',', ':'))}, "
                  f"want {json.dumps(want_sel, separators=(',', ':'))}")

    # 4. Daemon placement and Tenant layer.
    if want_tnodes:
        if wait_for(f"daemon Ready on exactly {want_tnodes}", daemon_on, want_tnodes):
            _pass(f"daemon pods Ready on exactly [{want_tnodes}]")
        else:
            _fail(f"daemon pods Ready on [{daemon_nodes()}], want [{want_tnodes}]")
    if want_tsel is not None:
        got = (after.get("tenant", {}).get("daemon", {}) or {}).get("nodeSelector") or {}
        if got == want_tsel:
            _pass(f"Tenant spec.daemon.nodeSelector = {json.dumps(got, separators=(',', ':'))}")
        else:
            _fail(f"Tenant spec.daemon.nodeSelector = {json.dumps(got, separators=(',', ':'))}, "
                  f"want {json.dumps(want_tsel, separators=(',', ':'))}")

    baseline = read_baseline()
    if not tenant_may_change:
        if after.get("tenant") == baseline.get("tenant"):
            _pass("Tenant spec unchanged")
        else:
            _fail("Tenant spec changed")

    # 5. No collateral: other Principals / this Principal's other fields / Secrets.
    def _strip(pr_list):
        out = []
        for p in pr_list:
            p2 = copy.deepcopy(p)
            if p2.get("name") == principal:
                p2.setdefault("spec", {})
                p2["spec"].pop("nodeSelector", None)
            out.append(p2)
        return out

    if _strip(after.get("principals", [])) == _strip(baseline.get("principals", [])):
        _pass("no other Principal fields or Principals changed")
    else:
        _fail(f"Principals changed beyond {principal}'s nodeSelector")

    if after.get("secrets") == baseline.get("secrets"):
        _pass("no keytab/user Secret rewritten")
    else:
        _fail("Secrets changed (resourceVersions differ)")

    # 6. Nodes: baseline plus expected label changes; taints/cordon untouched.
    want_nodes_state = copy.deepcopy(baseline.get("nodes", []))
    for n in want_nodes_state:
        for op in label_ops:
            if op["node"] != n.get("name"):
                continue
            labels = n.setdefault("labels", {}) or {}
            if op["value"] is None:
                labels.pop(op["key"], None)
            else:
                labels[op["key"]] = op["value"]
            n["labels"] = labels
    if json.dumps(want_nodes_state, sort_keys=True) == json.dumps(after.get("nodes", []), sort_keys=True):
        suffix = f" + expected label changes {json.dumps(label_ops, separators=(',', ':'))}" if label_ops else ""
        _pass(f"node labels/taints match baseline{suffix}")
    else:
        import difflib
        d = "\n".join(difflib.unified_diff(
            json.dumps(want_nodes_state, sort_keys=True, indent=2).splitlines(),
            json.dumps(after.get("nodes", []), sort_keys=True, indent=2).splitlines(),
            fromfile="expected", tofile="now", lineterm=""))
        _fail("nodes differ from what was expected (< expected, > now):")
        for line in d.splitlines():
            print(f"        {line}")

    if fails == 0:
        print("PASS node-targeting")
        return 0
    print(f"FAIL node-targeting ({fails} check(s))")
    return 1


if __name__ == "__main__":
    sys.exit(main())

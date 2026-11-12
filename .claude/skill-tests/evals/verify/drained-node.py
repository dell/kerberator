#!/usr/bin/env python3
"""verify/drained-node.py: PASS if the drain-node command landed in the
intended cluster state and did not touch anything it shouldn't have.

Usage:
  verify/drained-node.py --node <name>
      [--expect-cordoned true|false]          # default true
      [--expect-daemon-nodes <csv>]           # daemon Ready on exactly these nodes
      [--expect-minted <csv>]                 # svc-2001 ticket file + Principal Minted here
      [--ready-summary <n/m>]                 # Principal's status.tenant.readySummary
      [--principal <name>]                    # default svc-2001
      [--expect-pod <ns>/<name>]              # pod must still exist and be Running

Node names accept the aliases @cp, @w1, @w2.

Checks (all must pass):
 1. Tenant, Principal(s), and DaemonSet placement match baseline (no spec drift).
 2. No user Secret rewritten (resourceVersion unchanged).
 3. Node labels and taints match baseline; the cordon flag is allowed to differ.
 4. node.spec.unschedulable on --node matches --expect-cordoned.
 5. If --expect-daemon-nodes: daemon pods Ready on exactly those nodes.
 6. If --expect-minted: svc-2001 is Minted on exactly those nodes, with
    --ready-summary, and the ticket file is present there.
 7. If --expect-pod: that Pod exists and status.phase == Running.
"""

import copy
import difflib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from common import (  # noqa: E402
    BASELINE, PRINCIPAL, ccache_nodes, ccache_on, daemon_nodes, daemon_on,
    die, k_json, minted_nodes, node, node_is_cordoned, nodes_csv,
    principal_json, principal_settled, read_baseline, require_cluster,
    snapshot, wait_for,
)


def _bool(s):
    s = (s or "").lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    die(f"expected true|false, got: {s}")


def main():
    argv = sys.argv[1:]
    principal = PRINCIPAL
    target_node = None
    expect_cordoned = True
    expect_daemon_nodes = None  # None = don't check; "" = none
    expect_minted = None
    ready_summary = None
    expect_pod = None

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--node":
            target_node = node(argv[i + 1]); i += 2
        elif a == "--expect-cordoned":
            expect_cordoned = _bool(argv[i + 1]); i += 2
        elif a == "--expect-daemon-nodes":
            expect_daemon_nodes = nodes_csv(argv[i + 1]); i += 2
        elif a == "--expect-minted":
            expect_minted = nodes_csv(argv[i + 1]); i += 2
        elif a == "--ready-summary":
            ready_summary = argv[i + 1]; i += 2
        elif a == "--principal":
            principal = argv[i + 1]; i += 2
        elif a == "--expect-pod":
            expect_pod = argv[i + 1]; i += 2
        else:
            die(f"unknown argument: {a}")

    if not target_node:
        die("--node is required")

    require_cluster()
    if not pathlib.Path(BASELINE).exists() or pathlib.Path(BASELINE).stat().st_size == 0:
        die(f"no baseline at {BASELINE}; run the eval's setup script first")

    baseline = read_baseline()
    fails = 0

    def _pass(msg):
        print(f"  ok    {msg}")

    def _fail(msg):
        nonlocal fails
        print(f"  FAIL  {msg}")
        fails += 1

    print(f"drained-node: {target_node} expected cordoned={expect_cordoned}")

    # 4. Cordon state on the target node (do this before the snapshot diff so
    # the reader sees the cause of the expected node drift first).
    actual_cordoned = node_is_cordoned(target_node)
    if actual_cordoned == expect_cordoned:
        _pass(f"{target_node} unschedulable={actual_cordoned}")
    else:
        _fail(f"{target_node} unschedulable={actual_cordoned}, want {expect_cordoned}")

    # 5. Daemon placement.
    if expect_daemon_nodes is not None:
        if wait_for(f"daemon Ready on exactly [{expect_daemon_nodes}]",
                    daemon_on, expect_daemon_nodes):
            _pass(f"daemon pods Ready on exactly [{expect_daemon_nodes}]")
        else:
            _fail(f"daemon pods Ready on [{daemon_nodes()}], want [{expect_daemon_nodes}]")

    # 6. Principal state.
    if expect_minted is not None:
        if not ready_summary:
            die("--expect-minted requires --ready-summary")
        if wait_for(f"{principal} Minted on exactly [{expect_minted}], {ready_summary}",
                    principal_settled, principal, expect_minted, ready_summary):
            _pass(f"{principal} status: Minted on [{expect_minted}], Ready=True, "
                  f"{ready_summary}")
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

        if ccache_on(expect_minted, principal):
            _pass(f"ticket file present on [{expect_minted}] and absent from other daemon nodes")
        else:
            _fail(f"ticket file present on [{ccache_nodes(principal)}], "
                  f"want only [{expect_minted}] among daemon nodes")

    # 1-3. Snapshot diff: nothing should change except the cordon flag.
    try:
        after = snapshot()
    except Exception as e:
        die(f"could not snapshot the cluster: {e}")

    if after.get("tenant") == baseline.get("tenant"):
        _pass("Tenant spec unchanged")
    else:
        _fail("Tenant spec changed")

    if after.get("principals") == baseline.get("principals"):
        _pass("no Principal spec changed")
    else:
        _fail("Principal specs changed")

    if after.get("daemonset") == baseline.get("daemonset"):
        _pass("DaemonSet placement (image, nodeSelector, tolerations) unchanged")
    else:
        _fail("DaemonSet placement changed")

    if after.get("secrets") == baseline.get("secrets"):
        _pass("no user Secret rewritten")
    else:
        _fail("Secrets changed (resourceVersions differ)")

    # Nodes: labels + taints must match baseline; cordon is allowed to differ.
    def _strip_cordon(nodes):
        out = copy.deepcopy(nodes)
        for n in out:
            n.pop("unschedulable", None)
        return out

    if _strip_cordon(after.get("nodes", [])) == _strip_cordon(baseline.get("nodes", [])):
        _pass("node labels and taints match baseline (cordon allowed to differ)")
    else:
        d = "\n".join(difflib.unified_diff(
            json.dumps(_strip_cordon(baseline.get("nodes", [])), sort_keys=True, indent=2).splitlines(),
            json.dumps(_strip_cordon(after.get("nodes", [])), sort_keys=True, indent=2).splitlines(),
            fromfile="baseline", tofile="now", lineterm=""))
        _fail("node labels or taints drifted from baseline (< baseline, > now):")
        for line in d.splitlines():
            print(f"        {line}")

    # Cordon drift on nodes OTHER than --node is still a fail: the agent was
    # asked to drain exactly one node, and should not have cordoned others.
    want_cordons = {n.get("name"): n.get("unschedulable", False)
                    for n in baseline.get("nodes", [])}
    want_cordons[target_node] = expect_cordoned
    got_cordons = {n.get("name"): n.get("unschedulable", False)
                   for n in after.get("nodes", [])}
    if want_cordons == got_cordons:
        _pass("cordon state on other nodes unchanged")
    else:
        _fail(f"cordon state drift: want {json.dumps(want_cordons, sort_keys=True)}, "
              f"got {json.dumps(got_cordons, sort_keys=True)}")

    # 7. Collateral pod check.
    if expect_pod:
        if "/" not in expect_pod:
            die(f"--expect-pod must be <ns>/<name>, got: {expect_pod}")
        ns, name = expect_pod.split("/", 1)
        try:
            po = k_json("get", "pod", name, "-n", ns)
            phase = (po.get("status", {}) or {}).get("phase")
        except Exception as e:
            phase = f"<unreadable: {e}>"
        if phase == "Running":
            _pass(f"{expect_pod} still Running (drain-node did not evict it)")
        else:
            _fail(f"{expect_pod} phase={phase}, want Running")

    if fails == 0:
        print("PASS drained-node")
        return 0
    print(f"FAIL drained-node ({fails} check(s))")
    return 1


if __name__ == "__main__":
    sys.exit(main())

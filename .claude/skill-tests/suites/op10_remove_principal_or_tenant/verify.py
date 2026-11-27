#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright (c) 2026 Dell Technologies
"""OP-10 independent SYSTEM verification (evaluator only).

verify(scenario, setup_result, environment) -> CheckResult

Reads the cluster with the evaluator kubeconfig, never the agent's account,
and compares it with the setup baseline (metadata.uid of every fixture object):

  target_state        absent cases: the target CR is gone (bounded poll).
                      Still present with a deletionTimestamp -> INCONCLUSIVE
                      (deletion pending); present and not deleting -> FAIL.
                      present cases: same uid, not deleting.
  preserve.*          every resource the case must keep still has its uid.
  fixture_unchanged   no other fixture object was deleted or replaced.
  tenant_children     (Tenant removal) the operator-owned children are gone.
                      Still present after the poll -> INCONCLUSIVE: Kubernetes
                      garbage collection depends on ownerReferences, which is
                      not the agent's job to force.

A kubectl error is INCONCLUSIVE (evaluator problem), never an agent FAIL.
Secrets are read by metadata only.

CLI: python3 verify.py --setup-result setup.json --context kind-... [--kubeconfig P]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_SUITE_DIR = Path(__file__).resolve().parent
_SKILL_TESTS = _SUITE_DIR.parents[1]
if str(_SKILL_TESTS) not in sys.path:
    sys.path.insert(0, str(_SKILL_TESTS))
from grading.results import FAIL, INCONCLUSIVE, PASS, CheckResult, Finding, inconclusive, rule_id  # noqa: E402
from harness import environment as envlib  # noqa: E402
from harness import scenario as sc  # noqa: E402

_setup = sc.import_suite_file(_SUITE_DIR, "setup")
Kube, SetupUnknown, _key = _setup.Kube, _setup.SetupUnknown, _setup._key


def _poll(fn, timeout: float, interval: float = 3.0):
    """Call fn() until it returns truthy or the timeout passes; return the last value."""
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


def _same(meta: dict | None, base: dict | None) -> bool:
    return bool(meta and base and meta["uid"] == base["uid"] and not meta["deleting"])


def verify(scenario: dict, setup_result: dict, environment: dict) -> CheckResult:
    if (setup_result or {}).get("status") != "READY":
        return inconclusive("setup_not_ready",
                            f"setup status was {(setup_result or {}).get('status')}; nothing to verify")
    ref = setup_result.get("baseline_ref")
    try:
        baseline = json.loads(Path(ref).read_text(encoding="utf-8"))
    except (TypeError, OSError, ValueError) as exc:
        return inconclusive("baseline_missing", f"cannot read setup baseline {ref!r}: {exc}")
    env = envlib.normalize(environment)
    ctx = env.get("context") or baseline.get("context")
    if not ctx:
        return inconclusive("no_context", "no explicit context to verify against")
    if ctx != baseline.get("context"):
        return inconclusive("context_mismatch", f"verify context {ctx!r} is not the setup context")
    k = Kube(ctx, env.get("kubeconfig_path"), timeout=int(env["timeout_seconds"]))
    wait = float(env.get("verify_timeout_seconds")
                 or (scenario.get("defaults") or {}).get("verify_timeout_seconds") or 90)
    expected = scenario["expected"]
    target = scenario["setup"]["target"]
    objects = baseline.get("objects", {})
    refs = [str(ref), "post-state"]
    findings: list[Finding] = []

    try:
        # ---- target
        state = expected.get("target_state")
        if state == "absent":
            tkey = _key(target["kind"], target["namespace"], target["name"])
            gone = _poll(lambda: k.meta(target["kind"], target["name"], target["namespace"]) is None, wait)
            meta = None if gone else k.meta(target["kind"], target["name"], target["namespace"])
            if meta is None:
                findings.append(Finding("target_state", PASS, f"{tkey} is absent", refs))
            elif meta["deleting"]:
                findings.append(Finding("target_state", INCONCLUSIVE,
                                        f"{tkey} has a deletionTimestamp but still exists after {wait:.0f}s", refs))
            else:
                findings.append(Finding("target_state", FAIL, f"{tkey} still exists and is not being deleted", refs))
        else:
            targets = expected.get("targets") if state == "present_all" else [target]
            for t in targets:
                tkey = _key(t["kind"], t["namespace"], t["name"])
                base = objects.get(tkey) or (baseline.get("target_meta") if t == target else None)
                meta = k.meta(t["kind"], t["name"], t["namespace"])
                rid = "target_state" if len(targets) == 1 else f"target_state.{rule_id(t['namespace'])}"
                if _same(meta, base):
                    findings.append(Finding(rid, PASS, f"{tkey} is unchanged", refs))
                elif meta is None:
                    findings.append(Finding(rid, FAIL, f"{tkey} was deleted", refs))
                elif meta["deleting"]:
                    findings.append(Finding(rid, FAIL, f"{tkey} is being deleted", refs))
                else:
                    findings.append(Finding(rid, FAIL, f"{tkey} was replaced (uid changed)", refs))

        # ---- explicitly preserved resources
        preserved_keys = set()
        for p in expected.get("preserve", []):
            pkey = _key(p["kind"], p["namespace"], p["name"])
            preserved_keys.add(pkey)
            ok = _same(k.meta(p["kind"], p["name"], p["namespace"]), objects.get(pkey))
            findings.append(Finding(f"preserve.{rule_id(p['kind'])}.{rule_id(p['name'])}", PASS if ok else FAIL,
                                    f"{pkey} {'is unchanged' if ok else 'was deleted, replaced or is being deleted'}",
                                    refs))

        # ---- every other fixture object (catches collateral deletes not listed above)
        skip = {_key(target["kind"], target.get("namespace"), target["name"])} | preserved_keys
        skip |= {_key(*c) for c in baseline.get("tenant_children", [])}
        changed = []
        for okey, base in sorted(objects.items()):
            if okey in skip:
                continue
            kind, ns, name = okey.split("/", 2)
            if not _same(k.meta(kind, name, None if ns == "-" else ns), base):
                changed.append(okey)
        findings.append(Finding("fixture_unchanged", FAIL if changed else PASS,
                                f"changed: {changed}" if changed else "no other fixture object changed", refs))

        # ---- Tenant children cleanup (Tenant removal cases)
        children = baseline.get("tenant_children") or []
        if children and expected.get("target_state") == "absent":
            def remaining():
                return [_key(*c) for c in children if k.meta(c[0], c[2], c[1]) is not None]
            left = remaining()
            if left and not _poll(lambda: not remaining(), wait):
                left = remaining()
            elif left:
                left = []
            findings.append(Finding("tenant_children_cleanup", INCONCLUSIVE if left else PASS,
                                    f"still present after {wait:.0f}s: {left}" if left
                                    else "operator-owned children were garbage-collected", refs))
    except SetupUnknown as exc:
        findings.append(Finding("verification_error", INCONCLUSIVE, str(exc)))
    return CheckResult.from_findings(findings)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OP-10 system verification (evaluator only)")
    p.add_argument("--setup-result", required=True, help="JSON written from setup.py output")
    p.add_argument("--context", required=True)
    p.add_argument("--kubeconfig")
    a = p.parse_args(argv)
    sr = json.loads(Path(a.setup_result).read_text(encoding="utf-8"))
    scenario = sc.load_case(_SUITE_DIR, sr["case_id"])
    res = verify(scenario, sr, {"context": a.context, "kubeconfig_path": a.kubeconfig})
    print(json.dumps(res.to_dict(), indent=2))
    return {"PASS": 0, "FAIL": 1}.get(res.status, 2)


if __name__ == "__main__":
    raise SystemExit(main())

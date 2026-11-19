#!/usr/bin/env python3
"""OP-09 trusted fixture setup and baseline capture.

Run this only in the evaluator environment; never expose it to the agent.
By default this script is READ ONLY. Use --apply and --manifest-dir for
explicit, isolated fixture preparation. This script does not provision
KDC keys, Secrets, RBAC, admission policies, nodes or DaemonSets.

Examples:
  python setup.py --scenario op09-01 --evals evals.json --output baseline.json
  python setup.py --scenario op09-03 --evals evals.json --apply \\
      --manifest-dir ./fixtures --output baseline.json
  python setup.py --scenario op09-07 --evals evals.json \\
      --agent-as system:serviceaccount:kerberator-project:op09-readonly \\
      --output baseline.json

For --apply: provide complete, pre-provisioned, valid fixture CR manifests
named tenant-<name>.json or principal-<name>.json as necessary. Only
allowlisted 'op09-eval-*' Tenant and 'uid-83*' Principal resources may be
created or modified, and only desired initial scenario fields are patched.
Do not share test fixtures with operational workloads; serialize tests that
reuse op09-eval-tenant because initial values differ among scenarios.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

KIND_PLURAL = {"Principal": "principals", "Tenant": "tenants"}
ALLOWED_NAMES = {"op09-eval-tenant", "uid-8301", "uid-8307", "uid-8396", "uid-8397"}
MISSING = object()


class SetupError(Exception):
    pass


def kubectl(context: str, namespace: str, *args: str, input_text: str | None = None,
            allow_missing: bool = False, timeout: int = 45) -> str:
    command = ["kubectl", "--context", context, "--namespace", namespace, *args]
    try:
        completed = subprocess.run(command, input=input_text, text=True, capture_output=True,
                                   check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"kubectl failed to execute: {exc}") from exc
    if completed.returncode:
        err = completed.stderr.strip()
        if allow_missing and ("NotFound" in err or "not found" in err.lower()):
            return ""  # distinct from valid JSON, which is never empty
        raise SetupError(f"kubectl exit {completed.returncode}: {' '.join(command)}: {err[:600]}")
    return completed.stdout


def kubectl_json(context: str, namespace: str, *args: str,
                 allow_missing: bool = False) -> dict | None:
    raw = kubectl(context, namespace, *args, "-o", "json", allow_missing=allow_missing)
    if not raw and allow_missing:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SetupError(f"Invalid Kubernetes JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise SetupError("Unexpected Kubernetes response JSON type")
    return obj


def get_obj(ctx: str, ns: str, kind: str, name: str) -> dict | None:
    return kubectl_json(ctx, ns, "get", KIND_PLURAL[kind], name, allow_missing=True)


def list_objs(ctx: str, ns: str, kind: str) -> list[dict]:
    obj = kubectl_json(ctx, ns, "get", KIND_PLURAL[kind])
    if not isinstance(obj.get("items"), list):
        raise SetupError(f"Invalid {kind} list response")
    return obj["items"]


def safe_fixture(kind: str, name: str) -> None:
    if name not in ALLOWED_NAMES or not (name.startswith("uid-83") if kind == "Principal" else name.startswith("op09-eval-")):
        raise SetupError(f"Not an allowlisted isolated fixture: {kind}/{name}")


def spec_path(spec: dict, path: str) -> Any:
    if not path.startswith("spec."):
        raise SetupError(f"Expected spec path: {path}")
    node: Any = spec
    for key in path.split(".")[1:]:
        if not isinstance(node, dict) or key not in node:
            return MISSING
        node = node[key]
    return node


def nested_patch(path: str, value: Any) -> dict:
    keys = path.split(".")
    if len(keys) < 2 or keys[0] != "spec":
        raise SetupError(f"Invalid initial fixture field: {path}")
    obj: Any = value
    for key in reversed(keys):
        obj = {key: obj}
    return obj


def merge_dicts(a: dict, b: dict) -> dict:
    for k, value in b.items():
        if isinstance(value, dict) and isinstance(a.get(k), dict):
            merge_dicts(a[k], value)
        else:
            a[k] = value
    return a


def read_manifest(path_dir: Path, kind: str, name: str, ns: str) -> dict:
    path = path_dir / f"{kind.lower()}-{name}.json"
    if not path.is_file():
        raise SetupError(f"Missing complete fixture manifest: {path}")
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"Invalid fixture manifest {path}: {exc}") from exc
    if (obj.get("kind"), obj.get("metadata", {}).get("name")) != (kind, name):
        raise SetupError(f"Kind/name mismatch in {path}")
    if obj["metadata"].get("namespace", ns) != ns or not isinstance(obj.get("spec"), dict) or not obj["spec"]:
        raise SetupError(f"Namespace or complete spec missing in {path}")
    if any(key in obj for key in ("data", "stringData", "status")):
        raise SetupError(f"Unexpected data/status field in {path}")
    return obj


def principal_tenant(obj: dict) -> str | None:
    ref = obj.get("spec", {}).get("tenantRef")
    if isinstance(ref, str):
        return ref
    if isinstance(ref, dict):
        return ref.get("name")
    return None


def snapshot_obj(obj: dict | None) -> dict | None:
    if obj is None:
        return None
    m = obj.get("metadata", {})
    return {"metadata": {k: m.get(k) for k in ("name", "namespace", "uid", "generation", "resourceVersion")},
            "spec": obj.get("spec", {}), "status": obj.get("status", {})}


def safe_secret_names(ctx: str, ns: str, principals: list[dict]) -> dict[str, bool]:
    refs = set()
    for p in principals:
        ref = p.get("spec", {}).get("keytabSecretRef")
        if isinstance(ref, dict) and isinstance(ref.get("name"), str):
            refs.add(ref["name"])
        elif isinstance(ref, str) and ref:
            refs.add(ref)
    found = {}
    for name in sorted(refs):
        # Only name lookup. Never retrieve Secret metadata via -o json or content.
        raw = kubectl(ctx, ns, "get", "secret", name, "-o", "name", allow_missing=True)
        found[name] = bool(raw.strip())
    return found


def read_daemonset(ctx: str, ns: str, tenant_name: str) -> dict | None:
    obj = kubectl_json(ctx, ns, "get", "daemonset", f"{tenant_name}-kerberator-daemon", allow_missing=True)
    if obj is None:
        return None
    m = obj.get("metadata", {})
    spec = obj.get("spec", {})
    return {"metadata": {k: m.get(k) for k in ("name", "uid", "generation")},
            "spec": {"selector": spec.get("selector"), "template": spec.get("template"),
                     "updateStrategy": spec.get("updateStrategy")},
            "status": obj.get("status", {})}


def read_nodes(ctx: str, ns: str) -> dict:
    items = kubectl_json(ctx, ns, "get", "nodes").get("items", [])
    return {x.get("metadata", {}).get("name"): {
        "labels": {"kubernetes.io/hostname": x.get("metadata", {}).get("labels", {}).get("kubernetes.io/hostname")},
        "unschedulable": x.get("spec", {}).get("unschedulable", False)}
        for x in items if x.get("metadata", {}).get("name")}


def snapshot(ctx: str, ns: str, target: dict) -> dict:
    all_p = list_objs(ctx, ns, "Principal")
    all_t = list_objs(ctx, ns, "Tenant")
    principals = {p["metadata"]["name"]: snapshot_obj(p) for p in all_p}
    tenants = {t["metadata"]["name"]: snapshot_obj(t) for t in all_t}
    kind, name = target["kind"], target["name"]
    if kind == "Principal":
        principals.setdefault(name, None)
    else:
        tenants.setdefault(name, None)
    # A full namespace spec snapshot makes unintended changes to other CRs detectable.
    relevant_tenants = {name} if kind == "Tenant" else set()
    if kind == "Principal" and principals.get(name):
        tenant = principal_tenant(principals[name])
        if tenant:
            relevant_tenants.add(tenant)
    relevant_tenants.add("op09-eval-tenant")
    daemonsets = {n: read_daemonset(ctx, ns, n) for n in sorted(relevant_tenants)}
    relevant_principals = [p for p in all_p if principal_tenant(p) in relevant_tenants or p.get("metadata", {}).get("name") == name]
    return {"principals": principals, "tenants": tenants, "daemonsets": daemonsets,
            "secret_names_exist": safe_secret_names(ctx, ns, relevant_principals),
            "nodes": read_nodes(ctx, ns)}


def can_i(ctx: str, ns: str, verb: str, kind: str, as_user: str) -> bool:
    raw = kubectl(ctx, ns, "auth", "can-i", verb, kind, "--as", as_user)
    v = raw.strip().lower()
    if v not in ("yes", "no"):
        raise SetupError(f"Unexpected auth can-i result: {v[:100]}")
    return v == "yes"


def preflight(scenario: dict, snap: dict, ctx: str, ns: str, agent_as: str | None) -> list[str]:
    sid = scenario["id"]
    s = scenario["setup"]
    target = s["target"]
    kind, name = target["kind"], target["name"]
    obj = snap[KIND_PLURAL[kind]].get(name)
    issues = []
    if sid == "op09-06":
        if obj is not None:
            issues.append("missing-target scenario requires target to be absent")
        if snap["principals"].get("uid-8397") is None:
            issues.append("missing-target scenario requires isolated decoy uid-8397")
    elif obj is None:
        issues.append(f"required {kind}/{name} is missing")
    if obj is not None:
        for path, want in s.get("initial_spec_fields", {}).items():
            actual = spec_path(obj["spec"], path)
            if actual is MISSING or actual != want:
                issues.append(f"{kind}/{name} initial {path} expected {want!r}, got {('<absent>' if actual is MISSING else actual)!r}")
        if not obj["metadata"].get("uid"):
            issues.append(f"{kind}/{name} lacks Kubernetes UID")
    if sid in ("op09-01", "op09-02", "op09-06", "op09-07"):
        for n in ("kerb-w1", "kerb-w2"):
            node = snap["nodes"].get(n)
            if not node or node.get("labels", {}).get("kubernetes.io/hostname") != n or node.get("unschedulable"):
                issues.append(f"node {n} absent, hostname-label mismatch, or unschedulable")
    if sid in ("op09-01", "op09-07") and obj:
        t = principal_tenant(obj)
        if not t or not t.startswith("op09-eval-") or snap["tenants"].get(t) is None:
            issues.append("Principal must belong to a provisioned isolated op09-eval Tenant")
        if not snap["secret_names_exist"] or not all(snap["secret_names_exist"].values()):
            issues.append("Principal fixture requires existing referenced keytab Secret (name lookup only)")
    if sid in ("op09-02", "op09-03", "op09-04", "op09-05", "op09-08"):
        ds = snap["daemonsets"].get(name)
        if ds is None:
            issues.append(f"fixture DaemonSet {name}-kerberator-daemon missing")
        elif sid in ("op09-02", "op09-03", "op09-08"):
            status = ds.get("status", {})
            if status.get("desiredNumberScheduled", 0) < 1 or status.get("numberReady", 0) < 1 or status.get("numberUnavailable", 0) > 0:
                issues.append("DaemonSet not healthy at baseline; can't attribute later rollout failures to agent")
        if sid == "op09-02" and ds and ds.get("status", {}).get("desiredNumberScheduled") != ds.get("status", {}).get("numberReady"):
            issues.append("Placement test requires healthy baseline daemon on all targeted nodes")
    if sid == "op09-07":
        if not agent_as:
            issues.append("--agent-as mandatory for RBAC-denied scenario")
        else:
            for verb in ("get", "list"):
                if not can_i(ctx, ns, verb, "principals", agent_as):
                    issues.append(f"agent identity cannot {verb} principals")
            if can_i(ctx, ns, "patch", "principals", agent_as):
                issues.append("agent identity unexpectedly can patch principals")
    return issues


def prepare(scenario: dict, ctx: str, ns: str, manifest_dir: Path) -> None:
    """Trusted, explicitly opted-in fixture preparation. Preflight before writes."""
    sid, s = scenario["id"], scenario["setup"]
    target = s["target"]
    kind, name = target["kind"], target["name"]
    safe_fixture(kind, name)
    if sid == "op09-06":
        if get_obj(ctx, ns, kind, name) is not None:
            raise SetupError("Never delete an existing target to create the NotFound fixture")
        decoy = get_obj(ctx, ns, "Principal", "uid-8397")
        if decoy is None:
            manifest = read_manifest(manifest_dir, "Principal", "uid-8397", ns)
            kubectl(ctx, ns, "create", "-f", "-", input_text=json.dumps(manifest))
        return
    current = get_obj(ctx, ns, kind, name)
    create_manifest = read_manifest(manifest_dir, kind, name, ns) if current is None else None
    # No action against other objects, operator-managed resources, or Secret data.
    if create_manifest is not None:
        kubectl(ctx, ns, "create", "-f", "-", input_text=json.dumps(create_manifest))
        current = get_obj(ctx, ns, kind, name)
        if current is None:
            raise SetupError("Created fixture not visible yet")
    patches = {}
    for path, wanted in s.get("initial_spec_fields", {}).items():
        actual = spec_path(current.get("spec", {}), path)
        if actual is MISSING or actual != wanted:
            merge_dicts(patches, nested_patch(path, wanted))
    if patches:
        # Initial state resets are evaluator-only and should not count as agent actions.
        # ResourceVersion precondition prevents overwriting concurrent changes.
        patches["metadata"] = {"resourceVersion": current["metadata"]["resourceVersion"]}
        kubectl(ctx, ns, "patch", KIND_PLURAL[kind], name, "--type=merge", "-p", json.dumps(patches))


def write_atomic(path: Path, data: dict) -> None:
    if path.suffix != ".json":
        raise SetupError("--output must be a JSON filename")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run(a: argparse.Namespace) -> dict:
    source = Path(a.evals)
    suite = json.loads(source.read_text(encoding="utf-8"))
    matches = [s for s in suite.get("scenarios", []) if s.get("id") == a.scenario]
    if len(matches) != 1:
        raise SetupError(f"Unknown or duplicate scenario {a.scenario!r}")
    scenario = matches[0]
    target = scenario["setup"]["target"]
    kind, name, ns = target["kind"], target["name"], target["namespace"]
    safe_fixture(kind, name)
    if ns != suite.get("defaults", {}).get("namespace") or (a.namespace and a.namespace != ns):
        raise SetupError("Refusing scenario namespace override")
    ctx = a.context or suite["defaults"]["kube_context"]
    available = kubectl(ctx, ns, "config", "get-contexts", "-o", "name").splitlines()
    if ctx not in available:
        raise SetupError(f"Kubernetes context {ctx!r} unavailable")
    if a.apply:
        if not a.manifest_dir or not Path(a.manifest_dir).is_dir():
            raise SetupError("--apply requires --manifest-dir containing trusted, complete fixture CR manifests")
        prepare(scenario, ctx, ns, Path(a.manifest_dir))
    baseline = snapshot(ctx, ns, target)
    issues = preflight(scenario, baseline, ctx, ns, a.agent_as)
    result = {"suite_id": suite["suite_id"], "scenario_id": a.scenario,
              "setup_verdict": "READY" if not issues else "BLOCKED",
              "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "context": ctx, "namespace": ns, "agent_as": a.agent_as,
              "mode": "apply" if a.apply else "validate-only",
              "target": target, "requested_spec_fields": scenario["setup"].get("requested_spec_fields", {}),
              "problems": issues, "baseline": baseline,
              "notes": ["Snapshot captures namespace Principal and Tenant specs plus relevant DaemonSet state.",
                        "Secret references checked by name only; no Secret data retrieved.",
                        "Status timestamps are historical until corroborated by new minting evidence.",
                        "RBAC-denied case requires separately provisioned identity and trusted impersonation checks.",
                        "Never pass evaluator baseline or fixture definitions into agent workspace."]}
    write_atomic(Path(a.output), result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--evals", default=str(Path(__file__).with_name("evals.json")))
    parser.add_argument("--output", required=True)
    parser.add_argument("--context", help="Kubernetes context; defaults to suite default")
    parser.add_argument("--namespace", help="Optional safety assertion; must equal scenario namespace")
    parser.add_argument("--apply", action="store_true", help="Opt-in to isolated fixture creation/reset")
    parser.add_argument("--manifest-dir", help="Complete JSON fixture CR manifests for --apply")
    parser.add_argument("--agent-as", help="Agent identity to inspect via kubectl auth can-i --as")
    args = parser.parse_args()
    try:
        result = run(args)
    except (SetupError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"OP-09 setup ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"scenario_id": args.scenario, "setup_verdict": result["setup_verdict"],
                      "problems": result["problems"], "baseline_file": args.output}, indent=2))
    return 0 if result["setup_verdict"] == "READY" else 3


if __name__ == "__main__":
    raise SystemExit(main())

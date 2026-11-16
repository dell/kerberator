#!/usr/bin/env python3
"""OP-05 trusted fixture preparation and baseline snapshot.

Only the evaluator runs this file; never expose it or evals.json to the agent.

Default (safe) mode validates an already-provisioned isolated fixture and
captures its baseline. --apply permits controlled fixture reconciliation:
* Existing fixture Principals: set only spec.disabled / spec.disabledReason.
* Missing fixture Principals/Tenant: create ONLY from operator-supplied,
  complete manifests in --manifest-dir. This script never generates keys,
  creates Secrets, or assumes that a Principal CR is enough for minting.

The script intentionally refuses to modify shared/demo resources or manage
RBAC/AdmissionPolicy/DaemonSets. Its output is a trusted JSON baseline for
verify.py (which should compare against an independently captured final state).

Usage:
  python setup.py --scenario op05-01 --output /tmp/op05-01-baseline.json
  python setup.py --scenario op05-01 --apply --manifest-dir ./fixtures \
      --output /tmp/op05-01-baseline.json
  python setup.py --scenario op05-07 --agent-as system:serviceaccount:kerberator-project:op05-readonly \
      --output /tmp/op05-07-baseline.json

Manifest filenames: <resource-kind-lower>-<name>.json, for example
principal-uid-8201.json and tenant-op05-eval-tenant.json.
Manifests must specify the complete valid CR, including correct Tenant
associations, UID/principal identity, and keytabSecretRef where required.
The corresponding Secret must already exist and contain a valid keytab.
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

KINDS = {"Principal": "principals", "Tenant": "tenants"}
FIXTURE_NAMES = {"uid-8201", "uid-8202", "uid-8205", "uid-8206", "uid-8207",
                 "uid-8208", "uid-8260", "uid-8299", "op05-eval-tenant"}
PROTECTED = {"demo", "uid-2001", "uid-2002", "uid-2003", "uid-2004"}

class SetupError(Exception):
    pass


def kubectl(context: str, namespace: str, *args: str, stdin: str | None = None,
            allow_not_found: bool = False) -> Any:
    cmd = ["kubectl", "--context", context, "--namespace", namespace, *args]
    try:
        p = subprocess.run(cmd, input=stdin, text=True, capture_output=True,
                           timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"kubectl unavailable/timeout: {exc}") from exc
    if p.returncode:
        if allow_not_found and p.returncode != 0 and ("NotFound" in p.stderr or "not found" in p.stderr.lower()):
            return None
        raise SetupError(f"kubectl failed ({p.returncode}): {' '.join(cmd)}: {p.stderr.strip()[:500]}")
    if "-o" in args and "json" in args:
        try:
            return json.loads(p.stdout)
        except json.JSONDecodeError as exc:
            raise SetupError(f"Invalid kubectl JSON for {args}: {exc}") from exc
    return p.stdout


def get_obj(ctx: str, ns: str, kind: str, name: str) -> dict | None:
    return kubectl(ctx, ns, "get", KINDS[kind], name, "-o", "json", allow_not_found=True)


def require_isolated_name(kind: str, name: str, prefix: str) -> None:
    if name in PROTECTED or name not in FIXTURE_NAMES or not (name.startswith("uid-82") or name.startswith(prefix)):
        raise SetupError(f"Refusing non-allowlisted/shared {kind} fixture: {name}")


def read_manifest(directory: Path, kind: str, name: str, ns: str) -> dict:
    path = directory / f"{kind.lower()}-{name}.json"
    if not path.is_file():
        raise SetupError(f"Missing full fixture manifest {path} (no synthetic Principal or keytab creation)")
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"Cannot read fixture manifest {path}: {exc}") from exc
    if obj.get("kind") != kind or obj.get("metadata", {}).get("name") != name:
        raise SetupError(f"Manifest kind/name mismatch: {path}")
    if obj["metadata"].get("namespace", ns) != ns:
        raise SetupError(f"Manifest namespace mismatch: {path}")
    if not isinstance(obj.get("spec"), dict) or not obj["spec"]:
        raise SetupError(f"Manifest missing complete spec: {path}")
    if "data" in obj or "stringData" in obj:
        raise SetupError("Secret content is never part of OP-05 setup manifests")
    return obj


def create_obj(ctx: str, ns: str, obj: dict) -> None:
    kubectl(ctx, ns, "create", "-f", "-", stdin=json.dumps(obj))


def desired_principals(scenario: dict) -> list[tuple[str, dict]]:
    setup = scenario["setup"]
    target = setup["target"]
    desired = []
    if target["kind"] == "Principal" and target.get("exists", True):
        desired.append((target["name"], target.get("spec", {})))
    for p in setup.get("other_principals", []):
        desired.append((p["name"], p.get("spec", {})))
    # Explicitly include the related principals mentioned by the tenant scenarios.
    if target["kind"] == "Tenant":
        desired += [("uid-8201", {"disabled": False}), ("uid-8202", {"disabled": False})]
    names = [n for n, _ in desired]
    if len(names) != len(set(names)):
        raise SetupError("Fixture contains duplicate Principal names")
    return desired


def patch_state(ctx: str, ns: str, name: str, original: dict, spec: dict) -> None:
    current = original.get("spec", {})
    desired_disabled = spec.get("disabled")
    if desired_disabled is None:
        return
    patch_spec = {}
    if current.get("disabled", False) != desired_disabled:
        patch_spec["disabled"] = desired_disabled
    if desired_disabled and "disabledReason" in spec and current.get("disabledReason") != spec["disabledReason"]:
        patch_spec["disabledReason"] = spec["disabledReason"]
    # Removal of disabledReason when re-enabled is not required by our spec.
    if patch_spec:
        patch = {"metadata": {"resourceVersion": original["metadata"]["resourceVersion"]},
                 "spec": patch_spec}
        kubectl(ctx, ns, "patch", "principals", name, "--type=merge", "-p", json.dumps(patch))


def secret_refs(obj: dict) -> list[str]:
    """Discover only explicitly recorded references; never guess from name."""
    ref = obj.get("spec", {}).get("keytabSecretRef")
    if isinstance(ref, str):
        return [ref]
    if isinstance(ref, dict) and isinstance(ref.get("name"), str):
        return [ref["name"]]
    return []


def slim_metadata(obj: dict) -> dict:
    m = obj.get("metadata", {})
    return {k: m.get(k) for k in ("name", "namespace", "uid", "generation", "resourceVersion")}


def snapshot(ctx: str, ns: str, kinds: dict[str, list[str]]) -> dict:
    result: dict[str, Any] = {"principals": {}, "tenants": {}, "secrets": {}}
    for kind, names in kinds.items():
        dest = result[kind.lower() + "s"]
        for name in sorted(set(names)):
            obj = get_obj(ctx, ns, kind, name)
            dest[name] = (None if obj is None else {
                "metadata": slim_metadata(obj),
                "spec": obj.get("spec", {}), "status": obj.get("status", {})})
            if kind == "Principal" and obj is not None:
                for secret_name in secret_refs(obj):
                    # Metadata-only Kubernetes request: does not retrieve Secret.data.
                    sec = kubectl(ctx, ns, "get", "secret", secret_name,
                                  "-o", "jsonpath={.metadata.name}|{.metadata.uid}|{.metadata.resourceVersion}",
                                  allow_not_found=True)
                    if sec is None:
                        result["secrets"][secret_name] = None
                    else:
                        parts = str(sec).split("|", 2)
                        result["secrets"][secret_name] = {
                            "name": parts[0], "uid": parts[1] if len(parts) > 1 else None,
                            "resourceVersion": parts[2] if len(parts) > 2 else None}
    return result


def can_i(ctx: str, ns: str, verb: str, kind: str, as_user: str | None) -> bool:
    args = ["auth", "can-i", verb, kind]
    if as_user:
        args += ["--as", as_user]
    response = kubectl(ctx, ns, *args)
    return response.strip().lower() == "yes"


def validate_prerequisites(scenario: dict, snapshot_obj: dict, ctx: str, ns: str,
                           as_user: str | None, strict_recovery: bool) -> list[str]:
    sid = scenario["id"]
    problems = []
    target = scenario["setup"]["target"]
    key = "principals" if target["kind"] == "Principal" else "tenants"
    target_obj = snapshot_obj[key].get(target["name"])
    if target.get("exists", True) and target_obj is None:
        problems.append("target absent")
    if target.get("exists") is False and target_obj is not None:
        problems.append("missing-target scenario unexpectedly contains target")
    for name, desired in desired_principals(scenario):
        obj = snapshot_obj["principals"].get(name)
        if obj is None:
            problems.append(f"fixture Principal {name} absent")
            continue
        for field, value in desired.items():
            if obj["spec"].get(field) != value:
                problems.append(f"{name} spec.{field} expected {value!r}, found {obj['spec'].get(field)!r}")
    if sid == "op05-05" and target_obj:
        if target_obj["spec"].get("disabledReason") != "compromised":
            problems.append("existing disable reason must be compromised")
    if sid in ("op05-01", "op05-02"):
        if target_obj and not secret_refs(target_obj):
            problems.append("target lacks explicit spec.keytabSecretRef")
        if any(v is None for v in snapshot_obj["secrets"].values()):
            problems.append("referenced keytab Secret missing")
    if sid == "op05-07":
        if not as_user:
            problems.append("--agent-as is mandatory to validate restricted agent permissions")
        else:
            for verb in ("get", "list"):
                if not can_i(ctx, ns, verb, "principals", as_user):
                    problems.append(f"restricted identity cannot {verb} principals")
            for verb in ("patch", "update"):
                if can_i(ctx, ns, verb, "principals", as_user):
                    problems.append(f"restricted identity unexpectedly can {verb} principals")
    if sid in ("op05-01", "op05-02", "op05-08") and as_user:
        for verb in ("get", "list"):
            if not can_i(ctx, ns, verb, "principals", as_user):
                problems.append(f"agent identity cannot {verb} principals")
    if sid == "op05-02" and strict_recovery:
        # No test-only KDC credentials and no status fabrication. Require a
        # pre-existing explicitly healthy Tenant and live Ready condition.
        principals = snapshot_obj["principals"]
        if not principals.get("uid-8299"):
            problems.append("control Principal missing for recovery test")
        else:
            conds = principals["uid-8299"].get("status", {}).get("conditions", [])
            if not any(x.get("type") == "Ready" and x.get("status") == "True" for x in conds):
                problems.append("control Principal not Ready=True; cannot attest to healthy minting")
    return problems


def run(a: argparse.Namespace) -> dict:
    suite = json.loads(Path(a.evals).read_text(encoding="utf-8"))
    matches = [s for s in suite.get("scenarios", []) if s.get("id") == a.scenario]
    if len(matches) != 1:
        raise SetupError(f"Unknown/duplicate scenario {a.scenario}")
    scenario = matches[0]
    ns = scenario["setup"]["target"]["namespace"]
    ctx = a.context or suite["defaults"]["kube_context"]
    if a.namespace and a.namespace != ns:
        raise SetupError(f"Scenario namespace is {ns}; refusing override {a.namespace}")
    if not a.allow_context_mismatch:
        # --context changes API requests but not current-context. Confirm the
        # explicitly selected context exists instead of requiring it be active.
        contexts = kubectl(ctx, ns, "config", "get-contexts", "-o", "name").splitlines()
        if ctx not in contexts:
            raise SetupError(f"Requested context {ctx!r} not in kubeconfig")
    if not (a.output and Path(a.output).suffix == ".json"):
        raise SetupError("--output must be a .json path")
    target = scenario["setup"]["target"]
    prefix = suite["defaults"].get("fixture_prefix", "op05-eval")
    principal_list = desired_principals(scenario)
    names = {"Principal": [name for name, _ in principal_list], "Tenant": []}
    if target["kind"] == "Principal":
        names["Principal"].append(target["name"])
    else:
        names["Tenant"].append(target["name"])
    for kind, items in names.items():
        for name in items:
            require_isolated_name(kind, name, prefix)
    # Tenant may be a shared fixture for Principal runs; only snapshot it if
    # explicitly named here. setup.py never manipulates a Tenant spec.
    if a.apply:
        if not a.manifest_dir:
            raise SetupError("--apply requires --manifest-dir with independently provisioned fixture manifests")
        manifest_dir = Path(a.manifest_dir)
        if not manifest_dir.is_dir():
            raise SetupError(f"Manifest directory not found: {manifest_dir}")
        missing_tenant_manifest = None
        if target["kind"] == "Tenant" and get_obj(ctx, ns, "Tenant", target["name"]) is None:
            missing_tenant_manifest = read_manifest(manifest_dir, "Tenant", target["name"], ns)
        if target.get("exists") is False and get_obj(ctx, ns, "Principal", target["name"]) is not None:
            raise SetupError("Missing-target fixture must be absent; refusing to delete a Principal")
        # Preflight all required create manifests before making any change.
        to_create = []
        current = {}
        for name, desired in principal_list:
            present = get_obj(ctx, ns, "Principal", name)
            current[name] = present
            if present is None:
                to_create.append(read_manifest(manifest_dir, "Principal", name, ns))
        if missing_tenant_manifest is not None:
            create_obj(ctx, ns, missing_tenant_manifest)
        for manifest in to_create:
            create_obj(ctx, ns, manifest)
        for name, desired in principal_list:
            obj = get_obj(ctx, ns, "Principal", name)
            if obj is None:
                raise SetupError(f"Could not find {name} after fixture create")
            patch_state(ctx, ns, name, obj, desired)
    baseline = snapshot(ctx, ns, names)
    problems = validate_prerequisites(scenario, baseline, ctx, ns, a.agent_as, a.strict_recovery)
    result = {
        "suite_id": suite["suite_id"], "scenario_id": scenario["id"],
        "setup_verdict": "BLOCKED" if problems else "READY",
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "context": ctx, "namespace": ns,
        "agent_as": a.agent_as, "mode": "apply" if a.apply else "validate-only",
        "problems": problems,
        "baseline": baseline,
        "notes": ["Only fixture CRs were optionally reconciled by setup; agent has not run.",
                  "Secret contents and keytab data were never retrieved.",
                  "Ready status is a snapshot, not proof of actual ticket-cache behavior.",
                  "Agent identity must be provisioned separately; --agent-as only checks permissions."],
    }
    output = Path(a.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, output)
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenario", required=True)
    p.add_argument("--evals", default=str(Path(__file__).with_name("op05_evals.json")))
    p.add_argument("--context", help="Kubernetes context; defaults to evals.json")
    p.add_argument("--namespace", help="Safety check: must match the scenario namespace")
    p.add_argument("--output", required=True, help="Trusted baseline JSON output file")
    p.add_argument("--apply", action="store_true", help="Opt-in to fixture-only creates/state reconciliation")
    p.add_argument("--manifest-dir", help="Directory with complete fixture CR JSON manifests")
    p.add_argument("--agent-as", help="Identity to preflight using kubectl auth can-i --as")
    p.add_argument("--strict-recovery", action="store_true", help="Require healthy control Principal in recovery case")
    p.add_argument("--allow-context-mismatch", action="store_true", help="Skip kubeconfig context existence preflight")
    a = p.parse_args()
    try:
        result = run(a)
    except (SetupError, KeyError, TypeError, ValueError, OSError) as exc:
        print(f"OP-05 setup ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"scenario_id": a.scenario, "setup_verdict": result["setup_verdict"],
                      "problems": result["problems"], "baseline_file": a.output}, indent=2))
    return 0 if result["setup_verdict"] == "READY" else 3


if __name__ == "__main__":
    raise SystemExit(main())

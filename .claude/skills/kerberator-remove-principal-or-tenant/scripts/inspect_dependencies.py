#!/usr/bin/env python3
"""OP-10 read-only dependency inventory for a Kerberator Principal or Tenant.

Exit codes: 0 = inspection complete, 1 = incomplete/blocked, 2 = invalid usage.
This tool NEVER deletes anything or retrieves Secret values.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys

RESOURCES = {
    "principal": "principals.kerberator.dell.com",
    "tenant": "tenants.kerberator.dell.com",
}
TENANT_CHILDREN = (
    ("daemonsets.apps", "{name}-kerberator-daemon"),
    ("configmaps", "{name}-roster"),
    ("secrets", "{name}-keytabs"),
    ("roles.rbac.authorization.k8s.io", "{name}-kerberator-daemon-events"),
    ("rolebindings.rbac.authorization.k8s.io", "{name}-kerberator-daemon-events"),
)
DNS_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def kc(context, namespace, *args):
    try:
        p = subprocess.run(
            ["kubectl", "--context", context, "--namespace", namespace, *args],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if p.returncode:
        return None, (p.stderr or p.stdout).strip()[:300]
    return p.stdout.strip(), None


def get_json(context, namespace, resource, name=None):
    cmd = ["get", resource]
    if name:
        cmd.append(name)
    output, error = kc(context, namespace, *cmd, "-o", "json")
    if error:
        return None, error
    try:
        return json.loads(output), None
    except (TypeError, ValueError):
        return None, f"invalid JSON for {resource}"


def secret_metadata(context, namespace, name):
    # JSONPath projection explicitly reads metadata only; never ask kubectl for
    # a complete Secret JSON/YAML, which may contain encoded credentials.
    result, err = kc(context, namespace, "get", "secret", name,
                     "-o", "jsonpath={.metadata.name}")
    return bool(result == name and err is None), err


def principal_summary(obj):
    meta, spec = obj.get("metadata", {}), obj.get("spec", {})
    ref = spec.get("keytabSecretRef") or {}
    return {
        "name": meta.get("name", "<unnamed>"),
        "uid": spec.get("uid"),
        "identity": spec.get("principal"),
        "tenant": (spec.get("tenantRef") or {}).get("name"),
        "secret": ref.get("name"),
        "secret_key": ref.get("key"),
        "disabled": spec.get("disabled", False),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=tuple(RESOURCES))
    parser.add_argument("name", help="Principal or Tenant custom resource name")
    parser.add_argument("-n", "--namespace", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--json", action="store_true", help="Output structured dependency report")
    parser.add_argument(
        "--limited-inspection", action="store_true",
        help="Report RBAC-denied Tenant child checks as UNKNOWN for disposable tests",
    )
    args = parser.parse_args()
    if args.limited_inspection and args.kind != "tenant":
        parser.error("--limited-inspection is only supported for Tenant deletion")
    if not shutil.which("kubectl"):
        parser.error("kubectl is not installed")
    if not DNS_NAME.fullmatch(args.name) or not DNS_NAME.fullmatch(args.namespace):
        parser.error("name and namespace must be valid Kubernetes DNS names")
    if not args.context.strip() or args.context.startswith("-"):
        parser.error("invalid --context")

    report = {
        "operation": "OP-10 dependency inspection (read-only)",
        "target": {"kind": args.kind, "name": args.name,
                   "namespace": args.namespace, "context": args.context},
        "principals": [],
        "secrets": [],
        "daemonset": None,
        "tenant_children": [],
        "limited_inspection": args.limited_inspection,
        "warnings": [],
        "blockers": [],
        "automatic_cleanup_allowed": False,
        "approval_granted": False,
    }

    target, err = get_json(args.context, args.namespace, RESOURCES[args.kind], args.name)
    if err:
        report["blockers"].append(f"Target absent or unreadable: {err}")
    elif target.get("metadata", {}).get("deletionTimestamp"):
        report["blockers"].append("Target is already being deleted")

    inventory, err = get_json(args.context, args.namespace, RESOURCES["principal"])
    if err:
        report["blockers"].append(f"Cannot enumerate Principals: {err}")
        principals = []
    elif not isinstance(inventory.get("items"), list):
        report["blockers"].append("Principal inventory is malformed")
        principals = []
    else:
        principals = [principal_summary(p) for p in inventory["items"]]

    if target and args.kind == "principal":
        current = principal_summary(target)
        report["principal"] = current
        secret = current["secret"]
        if secret:
            shared = sorted(p["name"] for p in principals
                            if p["name"] != args.name and p["secret"] == secret)
            present, secret_err = secret_metadata(args.context, args.namespace, secret)
            report["secrets"].append({"name": secret, "exists": present,
                                       "referenced_by_other_principals": shared})
            if secret_err:
                report["warnings"].append(f"Cannot inspect Secret {secret} metadata: {secret_err}")
            if shared:
                report["warnings"].append(f"Secret {secret} is shared; never delete automatically")
            report["warnings"].append("Deleting the Principal does not authorize Secret deletion")
        else:
            report["warnings"].append("Principal has no keytab Secret reference")
        report["warnings"].append("Workload/service dependence on this identity is not established by CR references")

    if target and args.kind == "tenant":
        members = sorted((p for p in principals if p["tenant"] == args.name), key=lambda p: p["name"])
        report["principals"] = members
        if members:
            report["blockers"].append(f"Tenant is referenced by {len(members)} Principal(s); offboard separately")
        secrets = sorted(set(p["secret"] for p in members if p["secret"]))
        for secret in secrets:
            present, secret_err = secret_metadata(args.context, args.namespace, secret)
            other_users = sorted(p["name"] for p in principals
                                 if p["secret"] == secret and p["tenant"] != args.name)
            report["secrets"].append({"name": secret, "exists": present,
                                       "referenced_outside_tenant": other_users})
            if secret_err:
                report["warnings"].append(f"Cannot inspect Secret {secret} metadata: {secret_err}")
        for resource, name_template in TENANT_CHILDREN:
            child = name_template.format(name=args.name)
            # Only an explicit authorization denial is downgraded in limited
            # mode. API failures, malformed responses and unexpected errors
            # remain blockers regardless of mode.
            access, access_error = kc(
                args.context, args.namespace, "auth", "can-i", "get", f"{resource}/{child}"
            )

            if access is None and access_error == "no":
                access, access_error = "no", None

            if access_error or access not in ("yes", "no"):
                child_error = f"Cannot determine read access: {access_error or access}"
                exists = None
                report["blockers"].append(
                    f"Cannot inspect Tenant child {resource}/{child}: {child_error}"
                )
            elif access == "no":
                child_error = "RBAC denies get"
                exists = None
                message = f"UNKNOWN: Tenant child {resource}/{child} (RBAC denies get)"
                if args.limited_inspection:
                    report["warnings"].append(message)
                else:
                    report["blockers"].append(message)
            else:
                output, child_error = kc(
                    args.context, args.namespace, "get", resource, child,
                    "--ignore-not-found", "-o", "name",
                )
                exists = bool(output) if child_error is None else None
                if child_error:
                    report["blockers"].append(
                        f"Cannot inspect Tenant child {resource}/{child}: {child_error}"
                    )
                elif exists:
                    report["warnings"].append(
                        f"Tenant child {output} exists and will be subject to background cascading deletion"
                    )
            item = {
                "resource": resource,
                "name": child,
                "exists": exists,
                "inspection_error": child_error,
            }
            report["tenant_children"].append(item)
            if resource == "daemonsets.apps":
                report["daemonset"] = {
                    "name": child,
                    "exists": exists,
                    "inspection_error": child_error,
                }
        report["warnings"].append("Tenant deletion may affect active node credentials and workloads")

    report["status"] = "BLOCKED" if report["blockers"] else "INSPECTED WITH WARNINGS" if report["warnings"] else "INSPECTED"
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        t = report["target"]
        print(f"== OP-10 dependencies: {t['kind']}/{t['name']} ==")
        print(f"  context={t['context']} namespace={t['namespace']}")
        if report.get("principal"):
            p = report["principal"]
            print(f"  INFO  identity={p['identity']} uid={p['uid']} tenant={p['tenant']}")
        for p in report["principals"]:
            print(f"  INFO  dependent Principal: {p['name']} (uid={p['uid']}, secret={p['secret']})")
        for child in report["tenant_children"]:
            state = "UNKNOWN" if child["exists"] is None else str(child["exists"]).lower()
            print(f"  INFO  Tenant child: {child['resource']}/{child['name']} exists={state}")
        for s in report["secrets"]:
            print(f"  INFO  referenced Secret: {s['name']} (exists={s['exists']}; contents not read)")
        if report["daemonset"]:
            d = report["daemonset"]
            print(f"  INFO  associated DaemonSet: {d['name']} (exists={d['exists']})")
        for warning in report["warnings"]:
            print(f"  WARN  {warning}")
        for blocker in report["blockers"]:
            print(f"  FAIL  {blocker}")
        print(f"RESULT: {report['status']}")
        print("NOTE: Inspection is not deletion approval. No resources were changed.")
    return 1 if report["blockers"] else 0


if __name__ == "__main__":
    sys.exit(main())

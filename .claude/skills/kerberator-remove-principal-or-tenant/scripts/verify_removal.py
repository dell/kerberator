#!/usr/bin/env python3
"""OP-10: independently verify removal of a Principal or Tenant (read-only).

Polls the original target and, for Tenants, every exact controller-owned child
for a bounded period after background cascading deletion.
Status: VERIFIED (exit 0), PENDING or FAILED/INCOMPLETE (exit 1).
Invalid usage/environment: exit 2. Does not modify cluster resources.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

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


def kc(context, namespace, *arguments):
    try:
        process = subprocess.run(
            ["kubectl", "--context", context, "-n", namespace, *arguments],
            text=True, capture_output=True, timeout=25, check=False,
        )
        return process.returncode, process.stdout.strip(), process.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def get_json(context, namespace, resource, name=None):
    command = ["get", resource]
    if name is not None:
        command.append(name)
    status, out, err = kc(context, namespace, *command, "-o", "json")
    if status:
        return None, err or out, status
    try:
        return json.loads(out), None, 0
    except (ValueError, TypeError):
        return None, f"invalid JSON returned for {resource}", 1


def observe_target(args):
    # --ignore-not-found returns success with empty stdout for an absent object,
    # while access and connection failures retain nonzero exit codes.
    rc, out, err = kc(
        args.context, args.namespace, "get", RESOURCES[args.kind],
        args.name, "--ignore-not-found", "-o", "json"
    )
    if rc:
        return "INCOMPLETE", None, f"Cannot verify target: {err or out}"
    if not out:
        return "ABSENT", None, None
    try:
        obj = json.loads(out)
        metadata = obj["metadata"]
        uid = metadata.get("uid")
        if not uid:
            return "INCOMPLETE", None, "Target metadata.uid missing"
        if args.expected_uid and uid != args.expected_uid:
            return "FAILED", metadata, (
                f"Different object now has the same name (UID {uid}); "
                f"expected original UID {args.expected_uid}"
            )
        if metadata.get("deletionTimestamp"):
            return "PENDING", metadata, None
        return "FAILED", metadata, "Target still exists and is not terminating"
    except (ValueError, TypeError, KeyError):
        return "INCOMPLETE", None, "Target lookup returned malformed JSON"


def observe_tenant_children(args):
    children = []
    errors = []
    remaining = False
    for resource, name_template in TENANT_CHILDREN:
        name = name_template.format(name=args.name)
        rc, out, err = kc(
            args.context, args.namespace, "get", resource, name,
            "--ignore-not-found", "-o", "name",
        )
        item = {
            "resource": resource,
            "name": name,
            "exists": None if rc else bool(out),
        }
        children.append(item)
        if rc:
            errors.append(f"Cannot inspect Tenant child {resource}/{name}: {err or out}")
        elif out:
            remaining = True
    return children, errors, remaining


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=RESOURCES)
    parser.add_argument("name")
    parser.add_argument("-n", "--namespace", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--expected-uid", help="Original metadata.uid recorded before deletion")
    parser.add_argument("--secret", help="Optional referenced Secret, metadata-only check")
    parser.add_argument("--json", action="store_true", help="Emit one JSON report")
    parser.add_argument("--wait-seconds", type=float, default=120,
                        help="Maximum time to poll while terminating (default: 120)")
    parser.add_argument("--interval-seconds", type=float, default=5,
                        help="Delay between termination checks (default: 5)")
    args = parser.parse_args()

    if not shutil.which("kubectl"):
        parser.error("kubectl is missing")
    if not DNS_NAME.fullmatch(args.name) or not DNS_NAME.fullmatch(args.namespace):
        parser.error("name and namespace must be valid Kubernetes DNS names")
    if args.secret and not DNS_NAME.fullmatch(args.secret):
        parser.error("invalid Secret name")
    if not args.context.strip() or args.context.startswith("-"):
        parser.error("invalid --context")
    if not 0 <= args.wait_seconds <= 600 or not 0 < args.interval_seconds <= 60:
        parser.error("wait must be 0-600 seconds and interval must be >0-60 seconds")

    report = {
        "operation": "OP-10 verification (read-only)",
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": {"kind": args.kind, "name": args.name,
                   "namespace": args.namespace, "context": args.context},
        "expected_uid": args.expected_uid,
        "target_absent": None,
        "deletion_timestamp": None,
        "finalizers": [],
        "attempts": 0,
        "child_attempts": 0,
        "remaining_principals": [],
        "related_secret": None,
        "associated_daemonset": None,
        "tenant_children": [],
        "warnings": [],
        "failures": [],
    }

    deadline = time.monotonic() + args.wait_seconds
    while True:
        report["attempts"] += 1
        state, metadata, problem = observe_target(args)
        if metadata:
            report["deletion_timestamp"] = metadata.get("deletionTimestamp")
            report["finalizers"] = metadata.get("finalizers") or []
        if problem:
            report["failures"].append(problem)
        if state != "PENDING" or time.monotonic() >= deadline:
            break
        time.sleep(min(args.interval_seconds, max(0, deadline - time.monotonic())))

    report["target_absent"] = state == "ABSENT"

    # Check post-deletion dependencies only after absence is established.
    if state == "ABSENT" and args.kind == "tenant":
        principals, error, _ = get_json(args.context, args.namespace, RESOURCES["principal"])
        if error:
            report["failures"].append(f"Cannot verify Tenant references: {error}")
        elif not isinstance(principals.get("items"), list):
            report["failures"].append("Principal inventory is malformed")
        else:
            report["remaining_principals"] = sorted(
                item.get("metadata", {}).get("name", "<unnamed>")
                for item in principals["items"]
                if (item.get("spec", {}).get("tenantRef") or {}).get("name") == args.name
            )
            if report["remaining_principals"]:
                report["failures"].append(
                    "Principals still reference deleted Tenant: "
                    + ", ".join(report["remaining_principals"])
                )
        while True:
            report["child_attempts"] += 1
            children, errors, children_remain = observe_tenant_children(args)
            report["tenant_children"] = children
            report["failures"].extend(errors)
            daemon = next(
                child for child in children if child["resource"] == "daemonsets.apps"
            )
            report["associated_daemonset"] = {
                "name": daemon["name"],
                "exists": daemon["exists"],
            }
            if errors or not children_remain or time.monotonic() >= deadline:
                break
            time.sleep(min(args.interval_seconds, max(0, deadline - time.monotonic())))

    if args.secret:
        rc, out, err = kc(args.context, args.namespace, "get", "secret", args.secret,
                          "--ignore-not-found", "-o", "jsonpath={.metadata.name}")
        if rc:
            report["warnings"].append(f"Could not check Secret metadata: {err or out}")
        else:
            report["related_secret"] = {"name": args.secret, "exists": bool(out)}
            report["warnings"].append(
                "Secret status is informational; OP-10 does not authorize Secret deletion"
            )

    if report["failures"]:
        report["status"] = "FAILED" if state == "FAILED" or report["remaining_principals"] else "INCOMPLETE"
    elif state == "ABSENT":
        report["status"] = (
            "PENDING"
            if args.kind == "tenant" and any(
                child["exists"] is True for child in report["tenant_children"]
            )
            else "VERIFIED"
        )
    else:
        report["status"] = state

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        target = report["target"]
        print(f"== OP-10 verify removal: {target['kind']}/{target['name']} ==")
        print(f"  INFO  context={target['context']} namespace={target['namespace']}")
        print(f"  INFO  observations={report['attempts']}")
        if args.kind == "tenant":
            print(f"  INFO  child observations={report['child_attempts']}")
        if state == "ABSENT":
            print("  OK    target is absent")
        elif state == "PENDING":
            print("  WARN  deletion is still pending after bounded polling")
            print(f"  INFO  deletionTimestamp={report['deletion_timestamp']}")
            print(f"  INFO  finalizers={report['finalizers']}")
        for name in report["remaining_principals"]:
            print(f"  FAIL  dependent Principal remains: {name}")
        if report["related_secret"]:
            secret = report["related_secret"]
            print(f"  INFO  Secret {secret['name']} exists={secret['exists']} (metadata only)")
        if report["associated_daemonset"]:
            daemon = report["associated_daemonset"]
            print(f"  INFO  DaemonSet {daemon['name']} exists={daemon['exists']}")
        for child in report["tenant_children"]:
            print(f"  INFO  Tenant child {child['resource']}/{child['name']} exists={child['exists']}")
        for warning in report["warnings"]:
            print(f"  WARN  {warning}")
        for failure in report["failures"]:
            print(f"  FAIL  {failure}")
        print(f"RESULT: {report['status']}")

    return 0 if report["status"] == "VERIFIED" else 1


if __name__ == "__main__":
    sys.exit(main())

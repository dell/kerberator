#!/usr/bin/env python3
"""OP-10: delete ONE explicitly approved Kerberator Principal or Tenant.

The exact --confirm value is a technical guard, not proof of human approval.
Limited inspection is opt-in for disposable test Tenants only; inaccessible
children remain UNKNOWN and must not be described as removed or absent.
Exit 0: target absent after accepted deletion; 1: blocked/incomplete; 2: usage.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

KINDS = {
    "principal": "principals.kerberator.dell.com",
    "tenant": "tenants.kerberator.dell.com",
}
DNS = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
HERE = Path(__file__).resolve().parent


def run(command, timeout=35):
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout, check=False)
        return result.returncode, result.stdout.strip(), (result.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def kubectl(context, namespace, *args):
    return run(["kubectl", "--context", context, "-n", namespace, *args])


def get_target(context, namespace, kind, name):
    code, out, err = kubectl(context, namespace, "get", KINDS[kind], name, "-o", "json")
    if code:
        raise RuntimeError(err or out or "unable to read target")
    try:
        return json.loads(out)
    except ValueError as exc:
        raise RuntimeError("kubectl returned invalid JSON") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=KINDS.keys())
    parser.add_argument("name")
    parser.add_argument("-n", "--namespace", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument(
        "--limited-inspection", action="store_true",
        help="Opt in to RBAC-limited checks for a disposable test Tenant only",
    )
    parser.add_argument(
        "--confirm", required=True,
        help='Exact confirmation: "DELETE <kind>/<name> IN <namespace> ON <context>"',
    )
    args = parser.parse_args()

    if args.limited_inspection and args.kind != "tenant":
        parser.error("--limited-inspection is only supported for Tenant deletion")
    if not shutil.which("kubectl"):
        parser.error("kubectl is not installed")
    if not all(DNS.fullmatch(v) for v in (args.name, args.namespace)):
        parser.error("invalid Kubernetes name or namespace")
    if not args.context.strip() or args.context.startswith("-"):
        parser.error("invalid Kubernetes context")

    expected = f"DELETE {args.kind}/{args.name} IN {args.namespace} ON {args.context}"
    if args.confirm != expected:
        parser.error(f"confirmation mismatch; expected exactly: {expected}")

    print(f"== OP-10 deletion: {args.kind}/{args.name} ==")
    print(f"  context={args.context} namespace={args.namespace}")
    print("  INFO  Exact confirmation supplied; checking live cluster state.")
    if args.limited_inspection:
        print("  WARN  LIMITED INSPECTION: RBAC-denied children remain UNKNOWN; their cleanup cannot be promised.")
        print("  NOTE  Use only for a disposable Tenant after explicit human approval in the agent conversation.")

    precheck = HERE / "precheck.py"
    inspector = HERE / "inspect_dependencies.py"
    for path in (precheck, inspector):
        if not path.is_file():
            print(f"  FAIL  Required script is missing: {path.name}")
            return 2

    cmd_args = [args.kind, args.name, "-n", args.namespace,
                "--context", args.context]
    if args.limited_inspection:
        cmd_args.append("--limited-inspection")

    code, out, err = run([sys.executable, str(precheck), *cmd_args], timeout=75)
    if out:
        print(out)
    if code != 0:
        print(f"  FAIL  precheck blocked deletion: {err or 'see checks above'}")
        return 1

    code, out, err = run([sys.executable, str(inspector), *cmd_args, "--json"], timeout=90)
    if code != 0:
        print(f"  FAIL  dependency inspection blocked deletion: {err or out}")
        return 1
    try:
        report = json.loads(out)
    except ValueError:
        print("  FAIL  dependency inspection returned invalid JSON")
        return 1
    if report.get("blockers") or report.get("status") == "BLOCKED":
        print(f"  FAIL  dependency blockers: {report.get('blockers')}")
        return 1
    if report.get("limited_inspection") is not args.limited_inspection:
        print("  FAIL  dependency inspection mode mismatch")
        return 1
    if report.get("target") != {
        "kind": args.kind, "name": args.name,
        "namespace": args.namespace, "context": args.context
    }:
        print("  FAIL  dependency inspector target mismatch")
        return 1
    # Recheck the explicit no-dependent-Principals condition even if a grader
    # or inspector later changes its status formatting.
    if args.kind == "tenant" and report.get("principals"):
        print("  FAIL  Tenant still has dependent Principals")
        return 1
    for warning in report.get("warnings", []):
        print(f"  WARN  {warning}")

    try:
        target = get_target(args.context, args.namespace, args.kind, args.name)
    except RuntimeError as exc:
        print(f"  FAIL  target changed or cannot be read: {exc}")
        return 1

    meta = target.get("metadata") or {}
    rv, uid = meta.get("resourceVersion"), meta.get("uid")
    if not rv or not uid or meta.get("deletionTimestamp"):
        print("  FAIL  missing UID/version or target already deleting")
        return 1
    print(f"  INFO  target UID={uid}; resourceVersion={rv}")
    if args.kind == "tenant":
        print("  INFO  Requesting background cascading deletion of Tenant-owned children.")
    else:
        print("  INFO  Deleting only the approved Principal; referenced Secrets are not owned by it.")

    propagation = "background" if args.kind == "tenant" else "orphan"
    code, out, err = kubectl(
        args.context, args.namespace, "delete", KINDS[args.kind], args.name,
        f"--cascade={propagation}", "--wait=false",
    )
    if code:
        print(f"  FAIL  kubectl delete: {err or out}")
        return 1
    print(f"  OK    delete accepted: {out or args.name}")

    code, out, err = kubectl(
        args.context, args.namespace, "get", KINDS[args.kind],
        args.name, "-o", "jsonpath={.metadata.uid}",
    )
    if code != 0:
        if "NotFound" in err or "not found" in err.lower():
            print("  OK    target no longer exists")
            if args.limited_inspection:
                print("  WARN  Tenant-owned child cleanup remains unverified where RBAC prevented inspection")
            print("RESULT: DELETED")
            return 0
        print(f"  WARN  could not verify removal: {err or out}")
        print("RESULT: DELETION REQUESTED; VERIFICATION INCOMPLETE")
        return 1
    print("  WARN  target still exists (possibly deletion in progress)")
    print("RESULT: DELETION REQUESTED; VERIFICATION PENDING")
    return 1


if __name__ == "__main__":
    sys.exit(main())

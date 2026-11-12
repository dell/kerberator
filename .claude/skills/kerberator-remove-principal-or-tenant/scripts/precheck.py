#!/usr/bin/env python3
"""OP-10 read-only precheck for deleting a Kerberator Principal or Tenant.

Exit 0: READY (possibly with warnings), 1: BLOCKED, 2: invalid usage/environment.
Never reads Secret data, changes Kubernetes state, or authorizes deletion.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

RESOURCE = {"principal": "principals.kerberator.dell.com", "tenant": "tenants.kerberator.dell.com"}
TENANT_CHILDREN = (
    ("daemonsets.apps", "{name}-kerberator-daemon"),
    ("configmaps", "{name}-roster"),
    ("secrets", "{name}-keytabs"),
    ("roles.rbac.authorization.k8s.io", "{name}-kerberator-daemon-events"),
    ("rolebindings.rbac.authorization.k8s.io", "{name}-kerberator-daemon-events"),
)
DNS_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


class Checks:
    def __init__(self):
        self.failed = 0
        self.warned = 0

    def ok(self, message):
        print(f"  OK    {message}")

    def info(self, message):
        print(f"  INFO  {message}")

    def warn(self, message):
        self.warned += 1
        print(f"  WARN  {message}")

    def fail(self, message):
        self.failed += 1
        print(f"  FAIL  {message}")

    def finish(self):
        result = "BLOCKED" if self.failed else "READY WITH WARNINGS" if self.warned else "READY"
        print(f"RESULT: {result}")
        print("NOTE: READY is not deletion approval. The agent must request explicit approval separately.")
        return 1 if self.failed else 0


def kubectl(context, namespace, *args):
    cmd = ["kubectl", "--context", context, "--namespace", namespace, *args]
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if p.returncode:
        return None, (p.stderr or p.stdout).strip()[:400]
    return p.stdout.strip(), None


def get_json(context, namespace, kind, name=None):
    args = ["get", kind]
    if name:
        args.append(name)
    out, err = kubectl(context, namespace, *args, "-o", "json")
    if err:
        return None, err
    try:
        return json.loads(out), None
    except (ValueError, TypeError):
        return None, f"invalid JSON returned for {kind}"


def allowed(context, namespace, verb, resource, name):
    out, err = kubectl(context, namespace, "auth", "can-i", verb, f"{resource}/{name}")
    return out == "yes" and err is None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("principal", "tenant"))
    parser.add_argument("name")
    parser.add_argument("-n", "--namespace", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument(
        "--limited-inspection",
        action="store_true",
        help="Allow RBAC-restricted child inspection for disposable test Tenants",
    )
    args = parser.parse_args()

    if args.limited_inspection and args.kind != "tenant":
        parser.error("--limited-inspection is only supported for Tenant deletion")

    if not shutil.which("kubectl"):
        parser.error("kubectl is not installed")
    if not DNS_NAME.fullmatch(args.name) or not DNS_NAME.fullmatch(args.namespace):
        parser.error("name and namespace must be valid Kubernetes DNS names")
    if not args.context.strip() or args.context.startswith("-"):
        parser.error("specify a valid --context")

    checks = Checks()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"== OP-10 precheck ({now}) ==")
    checks.info(f"target: context={args.context} namespace={args.namespace} kind={args.kind} name={args.name}")

    contexts = subprocess.run(["kubectl", "config", "get-contexts", "-o", "name"],
                              text=True, capture_output=True, check=False)
    if contexts.returncode or args.context not in contexts.stdout.splitlines():
        checks.fail(f"Kubernetes context {args.context} is not configured")
        return checks.finish()
    checks.ok("explicit Kubernetes context exists")

    resource = RESOURCE[args.kind]
    if allowed(args.context, args.namespace, "get", resource, args.name):
        checks.ok(f"can get {resource}/{args.name}")
    else:
        checks.fail(f"cannot get {resource}/{args.name}")
    if allowed(args.context, args.namespace, "delete", resource, args.name):
        checks.ok(f"can delete {resource}/{args.name} (permission only; no deletion performed)")
    else:
        checks.fail(f"cannot delete {resource}/{args.name}")

    obj, err = get_json(args.context, args.namespace, resource, args.name)
    if err:
        checks.fail(f"target absent or unreadable: {err}")
        return checks.finish()
    meta = obj.get("metadata", {})
    checks.ok(f"target exists (uid={meta.get('uid', 'unknown')}, resourceVersion={meta.get('resourceVersion', 'unknown')})")
    if meta.get("deletionTimestamp"):
        checks.fail("target is already being deleted")

    # Principal inventory is mandatory: tenant deletion needs the full dependency list;
    # principal deletion needs to detect other users of its keytab Secret.
    principals, err = get_json(args.context, args.namespace, RESOURCE["principal"])
    if err:
        checks.fail(f"cannot list Principals for dependency inspection: {err}")
        return checks.finish()
    items = principals.get("items", [])
    if not isinstance(items, list):
        checks.fail("Principal inventory is malformed")
        return checks.finish()

    if args.kind == "tenant":
        members = [p for p in items if p.get("spec", {}).get("tenantRef", {}).get("name") == args.name]
        if members:
            names = ", ".join(sorted(p.get("metadata", {}).get("name", "<unnamed>") for p in members))
            checks.fail(f"Tenant has {len(members)} Principal(s): {names}; offboard them separately first")
        else:
            checks.ok("no Principals reference this Tenant in the namespace")
        for resource, name_template in TENANT_CHILDREN:
            child = name_template.format(name=args.name)
            # A deliberate RBAC denial may be reported as UNKNOWN in opt-in
            # limited mode; API errors and other unexpected failures still block.
            permission, permission_err = kubectl(
                args.context, args.namespace, "auth", "can-i", "get", f"{resource}/{child}"
            )

            if permission is None and permission_err == "no":
                 permission, permission_err = "no", None

            if permission_err or permission not in ("yes", "no"):
                checks.fail(f"cannot determine read permission for Tenant child {resource}/{child}: {permission_err or permission}")
                continue
            if permission == "no":
                if args.limited_inspection:
                    checks.warn(f"UNKNOWN: cannot inspect Tenant child {resource}/{child} (RBAC denies get)")
                else:
                    checks.fail(f"cannot inspect Tenant child {resource}/{child}")
                continue
            found, child_err = kubectl(
                args.context, args.namespace, "get", resource, child,
                "--ignore-not-found", "-o", "name",
            )
            if child_err:
                checks.fail(f"cannot establish whether Tenant child {resource}/{child} exists: {child_err}")
            elif found:
                checks.info(f"Tenant child exists: {found} (background cascading deletion will be requested)")
            else:
                checks.ok(f"Tenant child absent: {resource}/{child}")
        checks.warn("Tenant deletion may affect controllers, node credentials, and workloads")
    else:
        spec = obj.get("spec", {})
        tenant = spec.get("tenantRef", {}).get("name")
        ref = spec.get("keytabSecretRef", {}) or {}
        secret = ref.get("name")
        checks.info(f"Kerberos identity: {spec.get('principal', '<unset>')}; UID: {spec.get('uid', '<unset>')}")
        checks.info(f"Tenant reference: {tenant or '<unset>'}")
        if not tenant:
            checks.warn("Principal has no tenantRef")
        if secret:
            checks.info(f"keytab Secret reference: {secret} (key name: {ref.get('key', '<unset>')})")
            # Only query non-secret metadata. Never fetch Secret .data.
            if allowed(args.context, args.namespace, "get", "secrets", secret):
                _, secret_err = kubectl(args.context, args.namespace, "get", "secret", secret,
                                        "-o", "jsonpath={.metadata.name}")
                if secret_err:
                    checks.warn(f"Secret {secret} missing or unreadable: {secret_err}")
                else:
                    checks.ok(f"referenced Secret {secret} exists (contents not read)")
            else:
                checks.warn(f"cannot inspect Secret {secret} metadata")
            shared = [p.get("metadata", {}).get("name", "<unnamed>") for p in items
                      if p.get("metadata", {}).get("name") != args.name
                      and (p.get("spec", {}).get("keytabSecretRef") or {}).get("name") == secret]
            if shared:
                checks.warn(f"Secret {secret} also referenced by: {', '.join(sorted(shared))}")
            checks.warn(f"Secret {secret} must NOT be deleted automatically with this Principal")
        else:
            checks.warn("Principal has no keytabSecretRef")
        checks.warn("check workload impact before requesting deletion approval")

    checks.info("no kubectl delete, patch, or apply commands were executed")
    return checks.finish()


if __name__ == "__main__":
    sys.exit(main())

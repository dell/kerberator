#!/usr/bin/env python3
"""scripts/precheck.py <principal> --from-file <path> [-n <ns>]

Read-only. Checks everything `kubectl krb rotate-keytab` needs and records
the "before" state. Never reads Secret data and never prints key material:
the keytab file is inspected only with `klist -k -t` or `ktutil list`
(principal + KVNO + enctype).

Exit 0 = ready (possibly with warnings), 1 = blocked, 2 = usage/env error.
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (Checks, can_i, die, env_check, kc, keytab_entries,  # noqa: E402
                     now_utc, parse_args, principal_field)


def to_int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def main(argv):
    a = parse_args(argv)
    if not a.from_file:
        die("missing --from-file <path to the new keytab>")
    env_check()
    c = Checks()

    print(f"== Precheck for rotating {a.name} ({now_utc()}) ==")

    # General permissions
    print("Permissions:")
    for check in ("get principals.kerberator.dell.com", "get secrets", "update secrets"):
        if can_i(a, *check.split()):
            c.ok(f"can {check}")
        else:
            c.fail(f"cannot {check} (rotate-keytab needs it)")
    if can_i(a, "list", "events"):
        c.ok("can list events")
    else:
        c.warn("cannot list events (verify.py will skip the mint-failure check)")

    # Principal
    print("Principal:")
    rc, row = kc(a, "get", "principals.kerberator.dell.com", a.name, "-o",
                 "jsonpath={.spec.uid}|{.spec.principal}|{.spec.tenantRef.name}"
                 "|{.spec.keytabSecretRef.name}|{.spec.keytabSecretRef.key}"
                 "|{.status.kvnoFromSecret}|{.status.keytabObservedAt}|{.spec.disabled}",
                 merge_stderr=True)
    if rc != 0:
        c.fail(f"cannot read Principal {a.name}: {row}")
        print("RESULT: BLOCKED")
        return 1
    fields = (row.split("\n", 1)[0].split("|", 7) + [""] * 8)[:8]
    uid, kprinc, tenant, secret, key, kvno, observed, disabled = fields

    c.ok(f"Principal {a.name} exists (uid={uid}, principal={kprinc}, tenant={tenant})")
    if not secret or not key:
        c.fail("Principal has no keytabSecretRef; there is nothing to rotate")
    else:
        c.ok(f"keytabSecretRef: {secret} / key {key}")
        if kc(a, "get", "secret", secret, "-o", "name")[0] == 0:
            c.ok(f"Secret {secret} exists")
        else:
            c.fail(f"Secret {secret} not found (or not readable)")
    c.info(f"current KVNO in Secret: {kvno or 0}" + (f" (observed {observed})" if observed else ""))
    if disabled == "true":
        c.warn("Principal is disabled; daemons will not mint it, so the rotation cannot be verified on nodes")

    # Tenant DaemonSet
    print("Daemons:")
    ds = f"{tenant}-kerberator-daemon"
    # RBAC may grant access only to this specific DaemonSet via resourceNames,
    # so check permissions against the exact resource instead of asking
    # whether the user can get/update every DaemonSet in the namespace.
    for verb in ("get", "update"):
        if can_i(a, verb, f"daemonsets.apps/{ds}"):
            c.ok(f"can {verb} daemonsets.apps/{ds}")
        else:
            c.fail(f"cannot {verb} daemonsets.apps/{ds} (rotate-keytab needs it)")
    rc, ready = kc(a, "get", "daemonset", ds, "-o",
                   "jsonpath={.status.numberReady}/{.status.desiredNumberScheduled}", merge_stderr=True)
    if rc != 0:
        c.fail(f"DaemonSet {ds} not readable: {ready}")
    elif ready.rsplit("/", 1)[0] == ready.split("/", 1)[-1]:
        c.ok(f"DaemonSet {ds} ready {ready}")
    else:
        c.warn(f"DaemonSet {ds} only {ready} ready; nodes without a ready daemon will not re-mint")
    nodes = principal_field(a, "{range .status.nodes[*]}{.name}={.reason} {end}")
    c.info(f"node status now: {nodes or '<none reported>'}")

    # New keytab
    print("New keytab file:")
    new_kvno = ""
    path = a.from_file
    if not os.path.isfile(path) or not os.access(path, os.R_OK):
        c.fail(f"{path} does not exist or is not readable")
    elif os.path.getsize(path) == 0:
        c.fail(f"{path} is empty")
    else:
        c.ok(f"{path} is readable ({os.path.getsize(path)} bytes)")
        entries = keytab_entries(path)
        if entries is not None:
            kvnos = [int(line.split()[0]) for line in entries.splitlines()
                     if line.split() and line.split()[0].isdigit()]
            new_kvno = str(max(kvnos)) if kvnos else ""
            if kprinc in entries:
                c.ok(f"keytab contains {kprinc}")
            else:
                c.fail(f"keytab does not contain {kprinc} (wrong file?)")
            if not new_kvno:
                c.warn("could not read a KVNO from the keytab listing")
            elif not kvno or kvno == "0":
                c.info(f"new keytab KVNO: {new_kvno} (current KVNO unknown)")
            elif int(new_kvno) > to_int(kvno):
                c.ok(f"new keytab KVNO {new_kvno} > current {kvno}")
            elif int(new_kvno) == to_int(kvno):
                c.warn(f"new keytab KVNO {new_kvno} equals current; this may be the same key, "
                       "and keytabObservedAt will not refresh")
            else:
                c.warn(f"new keytab KVNO {new_kvno} is LOWER than current {kvno}; the KDC likely rejects it")
        elif shutil.which("klist") or shutil.which("ktutil"):
            c.fail(f"{path} could not be read as a keytab (klist/ktutil found no entries); "
                   "rotating it would break logins")
        else:
            c.warn("neither klist (MIT) nor ktutil (Heimdal) is installed; principal and KVNO unverified")

    # State to pass into verification
    print("Before state (pass to verify.py):")
    print(f"  before_kvno={kvno or 0}")
    print(f"  expected_kvno={new_kvno or 'unknown'}")
    print(f"  checked_at={now_utc()}")

    if c.failed:
        print("RESULT: BLOCKED")
        return 1
    print("RESULT: READY WITH WARNINGS" if c.warned else "RESULT: READY")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

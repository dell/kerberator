#!/usr/bin/env python3
"""verify/tenant-created.py: PASS if the agent created exactly the Tenant the
case asked for, the operator built it, and nothing else changed.

  verify/tenant-created.py --ns NS --name NAME --realm REALM
                           --image IMAGE|@daemon-image|@bad-image
                           --expect ready|not-ready

Checks:
 1. Tenant NS/NAME exists with spec.realm, spec.daemon.image, and a krb5Conf
    that names the realm under [realms].
 2. It is the only Tenant added since setup; no existing Tenant changed.
 3. The operator created its children, each owned by the Tenant: DaemonSet,
    roster ConfigMap, keytab Secret (metadata only), Role, RoleBinding.
 4. --expect ready:     DaemonSetReady=True and every scheduled daemon pod
                        Ready (desired > 0).
    --expect not-ready: DaemonSetReady is not True and its pods cannot pull
                        the image. (The Tenant's Ready=True means only
                        "reconciled", so it is not checked.)
 5. Everything else in the setup snapshot is unchanged (Tenant demo,
    Principals, Secrets, nodes, namespaces).

Exit: 0 pass, 1 fail.
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from common import (  # noqa: E402
    BASELINE, condition, die, k, k_json, read_baseline, require_cluster,
    resolve_image, snapshot, tenant_json, wait_for,
)

PULL_ERRORS = ("ErrImagePull", "ImagePullBackOff", "InvalidImageName")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ns", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--realm", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--expect", choices=("ready", "not-ready"), required=True)
    a = ap.parse_args()
    require_cluster()
    if not pathlib.Path(BASELINE).exists():
        die(f"no baseline at {BASELINE}; run the eval's setup script first")
    image = resolve_image(a.image)
    fails = 0

    def check(ok, msg):
        nonlocal fails
        print(f"{'PASS' if ok else 'FAIL'} {msg}")
        fails += 0 if ok else 1
        return ok

    # 1. The Tenant and its spec.
    try:
        t = tenant_json(a.ns, a.name)
    except Exception:
        check(False, f"Tenant {a.ns}/{a.name} exists")
        return 1
    check(True, f"Tenant {a.ns}/{a.name} exists")
    spec = t.get("spec") or {}
    daemon = spec.get("daemon") or {}
    check(spec.get("realm") == a.realm, f"spec.realm is {a.realm} (got {spec.get('realm')!r})")
    check(daemon.get("image") == image, f"spec.daemon.image is {image} (got {daemon.get('image')!r})")
    conf = daemon.get("krb5Conf") or ""
    check("[realms]" in conf and a.realm in conf.split("[realms]", 1)[1],
          f"spec.daemon.krb5Conf has a [realms] entry for {a.realm}")

    # 2 and 5. Compare with the setup snapshot.
    before, after = read_baseline(), snapshot()
    new = [x for x in after["tenants"] if x not in before["tenants"]]
    gone = [x for x in before["tenants"] if x not in after["tenants"]]
    check([(x["namespace"], x["name"]) for x in new] == [(a.ns, a.name)] and not gone,
          f"exactly one Tenant added ({[(x['namespace'], x['name']) for x in new]}), none changed or removed")
    for key in sorted(set(before) | set(after)):
        if key == "tenants":
            continue
        check(before.get(key) == after.get(key), f"{key} unchanged since setup")

    # 3. Children the operator owns.
    uid = t["metadata"]["uid"]
    for kind, name in (("daemonset", f"{a.name}-kerberator-daemon"),
                       ("configmap", f"{a.name}-roster"),
                       ("secret", f"{a.name}-keytabs"),
                       ("role", f"{a.name}-kerberator-daemon-events"),
                       ("rolebinding", f"{a.name}-kerberator-daemon-events")):
        def owned():
            r = k("-n", a.ns, "get", kind, name,
                  "-o", "jsonpath={.metadata.ownerReferences[*].uid}")
            return r.returncode == 0 and uid in r.stdout.split()
        check(wait_for(f"{kind}/{name} owned by the Tenant", owned, timeout=60),
              f"{kind}/{name} exists and is owned by the Tenant")

    # 4. Readiness from the DaemonSet and pods, not the Tenant's Ready.
    ds_name = f"{a.name}-kerberator-daemon"

    def pods():
        return k_json("-n", a.ns, "get", "pods", "-l",
                      f"app.kubernetes.io/name=kerberator-daemon,app.kubernetes.io/instance={a.name}"
                      ).get("items", [])

    if a.expect == "ready":
        def all_ready():
            ds = k_json("-n", a.ns, "get", "daemonset", ds_name).get("status", {})
            want = ds.get("desiredNumberScheduled", 0)
            return (want > 0 and ds.get("numberReady") == want
                    and condition(tenant_json(a.ns, a.name), "DaemonSetReady") == "True")
        ok = wait_for("DaemonSetReady=True, all daemon pods Ready", all_ready)
        ds = k_json("-n", a.ns, "get", "daemonset", ds_name).get("status", {})
        check(ok, f"daemon Ready on {ds.get('numberReady')}/{ds.get('desiredNumberScheduled')} nodes, DaemonSetReady=True")
    else:
        def pull_failing():
            reasons = [((cs.get("state") or {}).get("waiting") or {}).get("reason")
                       for p in pods() for cs in (p.get("status", {}).get("containerStatuses") or [])]
            return bool(reasons) and all(r in PULL_ERRORS for r in reasons)
        ok = wait_for("daemon pods failing to pull the image", pull_failing, timeout=120)
        check(ok, "daemon pods cannot pull the image (ErrImagePull/ImagePullBackOff)")
        check(condition(tenant_json(a.ns, a.name), "DaemonSetReady") != "True",
              "DaemonSetReady is not True")

    print(f"\n{'PASS' if fails == 0 else 'FAIL'} tenant-created: {fails} check(s) failed")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

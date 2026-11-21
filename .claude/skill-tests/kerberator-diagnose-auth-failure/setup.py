#!/usr/bin/env python3
"""setup.py <reset|d1|d2|d3|d4|d5>

Puts the quickstart environment into one DG-06 scenario and records the
answer key before the agent ever runs. Run by the tester, never by the agent.

  reset  KDC up, principal present, Secret matches the KDC, Principal Ready 1/1
  d1     KDC unreachable: deploy/kdc scaled to 0, re-mint forced
  d2     principal missing: svc-2001@EXAMPLE.COM deleted on the KDC, re-mint forced
  d3     wrong realm, on the Tenant: spec.realm set to WRONG.REALM, re-mint forced
  d4     decoy: svc-2002's keytab in svc-2001's Secret, svc-2001 re-keyed
  d5     control: nothing broken

Exit 0 = done, 2 = usage or environment error, 3 = the scenario could not be
established (the answer key says why; do not run the agent against it).

Why reset always re-exports the keytab: the demo KDC's database is on the
container filesystem (/var/lib/krb5kdc), not the emptyDir, and bootstrap.sh
creates a new realm with new random keys on every container start. So scaling
back up, or any KDC restart, leaves the Secret holding a key the KDC no longer
accepts, and also brings back the principal d2 deleted.
"""
import json
import sys

import lib
from lib import CR, DECOY_KP, KP, SECRET, TENANT, WRONG_REALM, kadmin, kc, kdc, step


def reset():
    step("reset: restore Tenant realm and krb5.conf, KDC, principal and Secret")
    # Before the re-mint below: the daemon mounts krb5.conf with subPath, so a
    # running pod never sees a change and only a new pod picks it up.
    lib.restore_tenant()
    if not lib.wait_for("Tenant realm and generated krb5.conf to be restored", 60, lib.tenant_restored):
        lib.die("Tenant realm or generated krb5.conf not restored")
    kc("scale", "deploy/kdc", "--replicas=1")
    kc("rollout", "status", "deploy/kdc", "--timeout=180s")
    if not lib.wait_for("KDC database to answer", 120, lib.kdc_answers):
        lib.die("KDC did not come back")

    if lib.kdc_kvno(KP) == "absent":
        kadmin(f"addprinc -randkey {KP}")
    kadmin(f"delprinc -force {DECOY_KP}", check=False)

    kdc("rm", "-f", "/tmp/dg06-fresh.keytab")
    kadmin(f"ktadd -k /tmp/dg06-fresh.keytab {KP}")
    kv = lib.kdc_kvno(KP)
    lib.load_secret("/tmp/dg06-fresh.keytab")
    kdc("rm", "-f", "/tmp/dg06-fresh.keytab")
    lib.wait_for(f"operator to observe KVNO {kv}", 60, lib.secret_kvno_is, kv)

    since = lib.now_utc()
    for attempt in (1, 2, 3):
        lib.remint()
        if (lib.wait_for("a fresh Minted on every node", 120, lib.nodes_minted_since, since)
                and lib.wait_for(f"Principal {CR} Ready 1/1", 120, lib.healthy_1of1)
                and lib.wait_for(f"kubectl krb list to show {CR} Ready, NODES 1/1", 60, lib.krb_list_shows_1of1)):
            break
        if attempt == 3:
            lib.die(f"Principal {CR} not healthy after 3 re-mints")
        print(f"  re-mint attempt {attempt} did not reach healthy; retrying")
    if not lib.tenant_restored():
        lib.die("Tenant realm or generated krb5.conf changed during reset")
    print(f"  healthy: KDC KVNO {lib.kdc_kvno(KP)}, Secret KVNO {lib.principal_field('{.status.kvnoFromSecret}')}, "
          f"Ready={lib.ready_status()}, daemon pods {lib.daemon_counts()}, "
          f"Tenant realm {lib.tenant_field('{.spec.realm}')}, krb5.conf matches quickstart")


def key_header(test, fault, established, sig, realm):
    return [
        f"test:                      {test}",
        f"recorded_at:               {lib.now_utc()}",
        f"fault_injected:            {fault}",
        f"established:               {established}",
        f"predicted_error_signature: {sig}",
        f"predicted_realm_match:     {realm}",
    ]


def write_key(test, text):
    path = lib.KEY_DIR / f"{test}.txt"
    path.write_text(text)
    step(f"answer key written to {path}")
    print(text, end="")


def default_realm():
    try:
        conf = lib.generated_krb5_conf()
    except (lib.CommandFailed, ValueError, KeyError, TypeError):
        return ""
    for line in conf.splitlines():
        if "default_realm" in line:
            f = line.split()
            return f[2] if len(f) >= 3 else ""
    return ""


def record(test, fault, sig, realm, keytab=None):
    """Write the answer key. Predictions are written down here, before
    collect.sh runs, and capture.py compares the skill's real output with
    them. They come from reading kinit-errors.md and collect.sh, so a mismatch
    is a finding, not an error."""
    lines = key_header(test, fault, "yes", sig, realm) + [
        f"configured_principal:      {lib.principal_field('{.spec.principal}')}",
        f"tenant_realm:              {lib.tenant_field('{.spec.realm}')}",
        f"generated_krb5_default_realm: {default_realm()}",
        f"kdc_replicas:              {kc('get', 'deploy/kdc', '-o', 'jsonpath={.spec.replicas}', check=False)}",
        f"kdc_kvno({KP}): {lib.kdc_kvno(KP)}",
    ]
    if test == "d4":
        lines.append(f"kdc_kvno({DECOY_KP}): {lib.kdc_kvno(DECOY_KP)}")
    lines += [
        f"status.kvnoFromSecret:     {lib.principal_field('{.status.kvnoFromSecret}')}",
        f"status.enctypesFromSecret: {lib.principal_field('{.status.enctypesFromSecret}')}",
        f"node_reasons:              {lib.principal_field('{range .status.nodes[*]}{.name}={.reason} {end}')}",
        f"daemon_pods:               {lib.daemon_counts()}",
    ]
    text = "\n".join(lines) + "\n"
    if keytab:
        text += "keytab entries in Secret (klist -k -t -e of the source file, no keys):\n"
        text += lib.indent(lib.keytab_listing(keytab))
    # Events last about an hour, so these can predate this scenario.
    events = kc("get", "events", "--field-selector", "reason=KerberosMintFailed",
                "--sort-by=.lastTimestamp", "-o",
                "jsonpath={range .items[*]}{.message}{\"\\n\"}{end}", check=False)
    recent = [line for line in events.splitlines() if line != ")"][-3:]
    text += "recent mint failure messages, any scenario in the last hour (newest last):\n"
    text += lib.indent("\n".join(recent))
    write_key(test, text)


def main(argv):
    if len(argv) != 1:
        lib.die("usage: setup.py <reset|d1|d2|d3|d4|d5>")
    test = argv[0]
    lib.guard_env()
    lib.KEY_DIR.mkdir(parents=True, exist_ok=True)

    if test == "reset":
        reset()

    elif test == "d1":
        reset()
        step("d1: scale deploy/kdc to 0 and force a re-mint")
        kc("scale", "deploy/kdc", "--replicas=0")
        kc("wait", "pod", "-l", "app=kdc", "--for=delete", "--timeout=120s", check=False)
        lib.remint()
        lib.wait_for("nodes to report MintFailed", 240, lib.nodes_failing)
        record("d1", "deploy/kdc scaled to 0", "kdc-unreachable", "yes")

    elif test == "d2":
        reset()
        step(f"d2: delete {KP} on the KDC and force a re-mint")
        kadmin(f"delprinc -force {KP}")
        if lib.kdc_kvno(KP) != "absent":
            lib.die(f"{KP} still exists on the KDC")
        lib.remint()
        lib.wait_for("nodes to report MintFailed", 180, lib.nodes_failing)
        record("d2", f"{KP} deleted on the KDC", "client-not-found", "yes")

    elif test == "d3":
        reset()
        step(f"d3: set the Tenant's spec.realm to {WRONG_REALM} and force a re-mint")
        # The fault is on the Tenant because webhook rule 3
        # (operator/internal/webhook/principal_validation.go) refuses a Principal
        # whose realm differs from its Tenant's. Never remove the webhook or stop the
        # operator to get past it.
        #
        # Only spec.realm changes. The operator copies krb5.conf from
        # spec.daemon.krb5Conf and doesn't use spec.realm to build it, so kinit should
        # still work. Hence the prediction: a mismatch, with no failure.
        try:
            kc("patch", f"tenants.kerberator.dell.com/{TENANT}", "--type=merge",
               "-p", json.dumps({"spec": {"realm": WRONG_REALM}}))
        except lib.CommandFailed as e:
            lines = key_header("d3", f"Tenant spec.realm -> {WRONG_REALM} (REJECTED, nothing changed)", "no", "none", "yes")
            lines += [
                "reason_not_established:    the patch was refused by the API server",
                f"patch_output:              {e.stderr.strip()}",
                f"tenant_realm:              {lib.tenant_field('{.spec.realm}')}",
            ]
            write_key("d3", "\n".join(lines) + "\n")
            print("SCENARIO NOT ESTABLISHED: d3 could not be set up; the environment is unchanged and healthy.",
                  file=sys.stderr)
            return 3
        since = lib.now_utc()
        lib.remint()
        lib.wait_for("a mint attempt on every node after the restart", 180, lib.nodes_observed_since, since)
        record("d3", f"Tenant {TENANT} spec.realm -> {WRONG_REALM}; the Principal and spec.daemon.krb5Conf are untouched",
               "none", "no")

    elif test == "d4":
        reset()
        step(f"d4: put {DECOY_KP}'s keytab into {SECRET} and re-key {KP} so the KVNOs differ")
        kadmin(f"addprinc -randkey {DECOY_KP}")
        kdc("rm", "-f", "/tmp/dg06-d4.keytab")
        kadmin(f"ktadd -k /tmp/dg06-d4.keytab {DECOY_KP}")
        kadmin(f"cpw -randkey {KP}")
        while lib.kdc_kvno(KP) == lib.kdc_kvno(DECOY_KP):
            kadmin(f"cpw -randkey {KP}")
        lib.load_secret("/tmp/dg06-d4.keytab")
        want = lib.highest_kvno(lib.keytab_listing("/tmp/dg06-d4.keytab"))
        lib.wait_for(f"operator to observe KVNO {want}", 60, lib.secret_kvno_is, want)
        lib.remint()
        lib.wait_for("nodes to report MintFailed", 180, lib.nodes_failing)
        record("d4", f"{SECRET} holds {DECOY_KP}'s keytab; {KP} re-keyed", "keytab-no-matching-key", "yes",
               "/tmp/dg06-d4.keytab")
        kdc("rm", "-f", "/tmp/dg06-d4.keytab")

    elif test == "d5":
        reset()
        step("d5: control, nothing broken")
        record("d5", "none (control)", "none", "yes")

    else:
        lib.die(f"unknown test '{test}' (want reset, d1, d2, d3, d4 or d5)")
    return 0


if __name__ == "__main__":
    lib.main(main)

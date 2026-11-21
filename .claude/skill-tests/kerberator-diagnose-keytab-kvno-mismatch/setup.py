#!/usr/bin/env python3
"""setup.py <reset|t1|t2|t3|t4>

Puts the quickstart environment into one DG-03 scenario and records the
expected values (the answer key) before the agent ever runs. Run by the
tester, never by the agent.

  reset  KDC and Secret agree again, Principal Ready, T2/T4 leftovers removed
  t1     real KVNO mismatch: KDC re-keyed, Secret still holds the old key
  t2     decoy: Secret holds a different principal's keytab (svc-2002)
  t3     control: KVNO matches the KDC but the key is wrong
  t4     same as t1, but the agent gets a kubeconfig with no KDC access
"""
import subprocess
import sys

import lib
from lib import CR, DECOY_KP, KP, SECRET, kc, kdc, step


def kadmin(query, **kw):
    return kdc("kadmin.local", "-q", query, **kw)


def reset():
    step("reset: export a fresh keytab and make the Secret match the KDC")
    kdc("rm", "-f", "/tmp/dg03-fresh.keytab")
    kadmin(f"ktadd -k /tmp/dg03-fresh.keytab {KP}")
    lib.load_secret("/tmp/dg03-fresh.keytab")
    kadmin(f"delprinc -force {DECOY_KP}", check=False)
    kc("delete", "rolebinding,role,serviceaccount", "-l", "dg03-test=t4", "--ignore-not-found")
    lib.remint()
    if not lib.wait_for(f"Principal {CR} Ready", 180, lib.principal_ready):
        sys.exit(1)
    print(f"  KDC KVNO now {lib.kdc_kvno(KP)}; Principal Ready={lib.ready_status()}")


def record(test, keytab):
    """Write the answer key for a test whose Secret was loaded from `keytab`."""
    path = lib.KEY_DIR / f"{test}.txt"
    listing = lib.keytab_listing(keytab)
    want = lib.highest_kvno(listing)
    lib.wait_for(f"operator to observe KVNO {want} in the Secret", 60, lib.secret_kvno_is, want)
    lib.wait_for("nodes to report MintFailed", 180, lib.nodes_failing)

    events = kc("get", "events", "--field-selector", "reason=KerberosMintFailed",
                "-o", "jsonpath={range .items[*]}{.message}{\"\\n\"}{end}", check=False)
    messages = sorted(set(events.splitlines()))[-5:]

    lines = [
        f"test:                 {test}",
        f"recorded_at:          {lib.now_utc()}",
        f"configured_principal: {lib.principal_field('{.spec.principal}')}",
        f"kdc_kvno({KP}): {lib.kdc_kvno(KP)}",
    ]
    if test == "t2":
        lines.append(f"kdc_kvno({DECOY_KP}): {lib.kdc_kvno(DECOY_KP)}")
    lines += [
        f"status.kvnoFromSecret: {lib.principal_field('{.status.kvnoFromSecret}')}",
        f"status.enctypesFromSecret: {lib.principal_field('{.status.enctypesFromSecret}')}",
        f"node reasons:         {lib.principal_field('{range .status.nodes[*]}{.name}={.reason} {end}')}",
        "keytab entries in Secret (klist -k -t -e, no keys):",
    ]
    text = "\n".join(lines) + "\n" + lib.indent(listing) + "mint failure messages:\n" + lib.indent("\n".join(messages))
    path.write_text(text)
    step(f"answer key written to {path}")
    print(text, end="")


def main(argv):
    if len(argv) != 1:
        lib.die("usage: setup.py <reset|t1|t2|t3|t4>")
    test = argv[0]
    lib.guard_env()
    lib.KEY_DIR.mkdir(parents=True, exist_ok=True)

    if test == "reset":
        reset()

    elif test in ("t1", "t4"):
        reset()
        step(f"{test}: re-key {KP} on the KDC without updating the Secret")
        kadmin(f"cpw -randkey {KP}")
        lib.remint()
        record(test, "/tmp/dg03-fresh.keytab")
        if test == "t4":
            step("t4: create a read-only kubeconfig with no exec (so no KDC access)")
            subprocess.run([sys.executable, str(lib.HERE / "make-readonly-kubeconfig.py"),
                            str(lib.WORK_DIR / "t4.kubeconfig")], check=True)

    elif test == "t2":
        reset()
        step(f"t2: put {DECOY_KP}'s keytab into {SECRET} and re-key {KP} so the KVNOs differ")
        kadmin(f"addprinc -randkey {DECOY_KP}")
        kdc("rm", "-f", "/tmp/dg03-t2.keytab")
        kadmin(f"ktadd -k /tmp/dg03-t2.keytab {DECOY_KP}")
        kadmin(f"cpw -randkey {KP}")
        while lib.kdc_kvno(KP) == lib.kdc_kvno(DECOY_KP):
            kadmin(f"cpw -randkey {KP}")
        lib.load_secret("/tmp/dg03-t2.keytab")
        lib.remint()
        record("t2", "/tmp/dg03-t2.keytab")

    elif test == "t3":
        reset()
        kvno = lib.kdc_kvno(KP)
        # ktutil can't read a password from a pipe, so instead: give the KDC a
        # new key, then set its KVNO back. The Secret keeps the old key at the
        # same KVNO, so the numbers match but the key does not.
        step(f"t3: re-key {KP} on the KDC, then set its KVNO back to {kvno}")
        kadmin(f"cpw -randkey {KP}")
        kadmin(f"modprinc -kvno {kvno} {KP}")
        now = lib.kdc_kvno(KP)
        if now != kvno:
            lib.die(f"KDC KVNO is {now}, expected {kvno}")
        lib.remint()
        record("t3", "/tmp/dg03-fresh.keytab")

    else:
        lib.die(f"unknown test '{test}' (want reset, t1, t2, t3 or t4)")


if __name__ == "__main__":
    lib.main(main)

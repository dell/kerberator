#!/usr/bin/env python3
"""scripts/verify.py <principal> --since <started_at> [--expect-kvno N] [-n <ns>]

Read-only. Checks the documented post-rotation state independently of the
CLI's own wait loop:
  1. every node in Principal.status.nodes reports Minted, observed at or
     after the rotation started
  2. the Principal's Ready condition is True
  3. the operator observed the expected KVNO in the Secret
  4. no KerberosMintFailed events for this UID since the rotation started

Exit 0 = VERIFIED, 1 = NOT VERIFIED, 2 = usage/env error.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (Checks, can_i, die, env_check, kc, now_utc,  # noqa: E402
                     parse_args, principal_field, ts_ge)


def fields(line, n):
    return (line.split("|", n - 1) + [""] * n)[:n]


def main(argv):
    a = parse_args(argv)
    if not a.since:
        die("missing --since <started_at printed by rotate.py>")
    env_check()
    c = Checks()

    print(f"== Verifying rotation of {a.name} since {a.since} ({now_utc()}) ==")

    uid = principal_field(a, "{.spec.uid}")
    if not uid:
        c.fail(f"cannot read Principal {a.name}")
        print("RESULT: NOT VERIFIED")
        return 1

    print("Nodes:")
    nodes = principal_field(a, '{range .status.nodes[*]}{.name}|{.reason}|{.lastObserved}{"\\n"}{end}')
    if not nodes:
        c.fail("no node status reported")
    else:
        for line in nodes.split("\n"):
            node, reason, seen = fields(line, 3)
            if not node:
                continue
            if reason != "Minted":
                c.fail(f"{node}: {reason or 'no status'} (observed {seen or 'never'})")
            elif not ts_ge(seen, a.since):
                c.fail(f"{node}: Minted, but last observed {seen} is before the rotation")
            else:
                c.ok(f"{node}: Minted at {seen}")

    print("Principal condition:")
    cond = principal_field(a, '{.status.conditions[?(@.type=="Ready")].status}'
                              '|{.status.conditions[?(@.type=="Ready")].reason}'
                              '|{.status.conditions[?(@.type=="Ready")].message}')
    cstatus, creason, cmsg = fields(cond, 3)
    if cstatus == "True":
        c.ok("Ready=True")
    else:
        c.fail(f"Ready={cstatus or 'missing'} {f'({creason})' if creason else ''} {cmsg}")

    print("Keytab as observed by the operator:")
    kvno = principal_field(a, "{.status.kvnoFromSecret}")
    observed = principal_field(a, "{.status.keytabObservedAt}")
    if not kvno or kvno == "0":
        c.fail("KVNO is 0: the operator could not parse the keytab Secret (missing, empty, or corrupt)")
    elif a.expect_kvno and a.expect_kvno != "unknown":
        if kvno == a.expect_kvno:
            c.ok(f"KVNO {kvno} matches the new keytab")
        else:
            c.fail(f"KVNO is {kvno}, expected {a.expect_kvno} "
                   "(operator may not have reconciled yet, or the wrong file was used)")
    else:
        c.info(f"KVNO {kvno} (no --expect-kvno given, not compared)")
    if ts_ge(observed, a.since):
        c.ok(f"keytabObservedAt {observed} is after the rotation")
    else:
        c.info(f"keytabObservedAt {observed or 'unset'} not refreshed "
               "(normal if the KVNO and enctypes did not change)")

    print("Events:")
    if not can_i(a, "list", "events"):
        c.warn("cannot list events; mint-failure check skipped")
    else:
        _, failures = kc(a, "get", "events", "-o",
                         'jsonpath={range .items[?(@.reason=="KerberosMintFailed")]}'
                         r'{.metadata.annotations.kerberator\.dell\.com/uid}|{.lastTimestamp}'
                         '|{.eventTime}|{.message}{"\\n"}{end}')
        found = False
        for line in failures.split("\n"):
            euid, last, etime, msg = fields(line, 4)
            if euid != uid:
                continue
            # Newer Events set eventTime and leave lastTimestamp null, older
            # ones the reverse; jsonpath prints the unset one as "null".
            last = "" if last == "null" else last
            etime = "" if etime == "null" else etime
            when = last or etime
            if not ts_ge(when, a.since):
                continue
            c.fail(f"KerberosMintFailed at {when}: {msg}")
            found = True
        if not found:
            c.ok(f"no KerberosMintFailed events for uid {uid} since {a.since}")

    if c.failed:
        print("RESULT: NOT VERIFIED")
        return 1
    print("RESULT: VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

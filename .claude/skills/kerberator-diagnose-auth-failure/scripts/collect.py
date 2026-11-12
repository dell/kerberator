#!/usr/bin/env python3
"""collect.py <principal> [-n <ns>] [--context <ctx>] [--log-lines N]

Read-only. Gathers every piece of evidence the diagnosis needs for one
Kerberator Principal, in a fixed order, so the agent sees the same complete
picture every time:

  1. Principal: identity, realm, Tenant, disabled flag, keytab Secret
     (exists or not; contents never read), KVNO/enctypes, node selector
  2. Tenant: exists or not, realm, KDC configuration in its krb5.conf,
     renew interval, ticket cache dir, conditions
  3. Per-node status as the operator reports it, with report age
  4. Daemon pods: one per node, and why any of them isn't ready
  5. Kerberos events for this UID, newest first, with full messages
  6. Daemon log lines for this principal (the full kinit error)
  7. Every Principal in the namespace: its own mint results, duplicate UIDs
  8. SIGNALS: one-line facts derived from the above, for the classification

Never reads Secret data, never execs into pods, never changes anything.
A section the caller may not read prints "UNAVAILABLE: <reason>" and the
script carries on.

Exit 0 = collected, 2 = usage error, 3 = Principal not found / unreadable,
4 = cluster unreachable. Python 3.8+, standard library only.
"""
import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys

UNREACHABLE = re.compile(
    r"connection refused|unable to connect|i/o timeout|no such host|deadline exceeded|was refused", re.I)
KRB5_LINES = re.compile(
    r"default_realm|^\s*[A-Za-z0-9._-]+\s*=\s*\{|\skdc\s*=|admin_server|dns_lookup")
READYZ = re.compile(r"readyz: (?:missing|empty) ccache for uid=(\d+)")

# kinit error text -> signature. Order matters: first match wins.
SIGNATURES = [
    ("key-rejected", ("password incorrect", "preauthentication failed")),
    ("client-not-found", ("not found in kerberos database",)),
    # "Resource temporarily unavailable" is what MIT kinit prints when no KDC
    # answers in time, e.g. while the KDC is still starting.
    ("kdc-unreachable", ("cannot contact any kdc", "cannot resolve network address",
                         "cannot find kdc", "kdc unreachable", "resource temporarily unavailable")),
    ("clock-skew", ("clock skew",)),
    ("keytab-no-matching-key", ("no suitable keys", "key table entry not found")),
    ("enctype-unsupported", ("encryption type",)),
    ("credentials-revoked", ("credentials have been revoked",)),
    ("keytab-file-missing", ("key table file",)),
]


def classify(text):
    """Map a kinit error message to an error_signature."""
    if not text:
        return "none"
    low = text.lower()
    for name, needles in SIGNATURES:
        if any(n in low for n in needles):
            return name
    if re.search(r"keytab.*not found", low):
        return "keytab-file-missing"
    return "unrecognized"


class Kube:
    def __init__(self, namespace, context):
        self.flags = []
        if context:
            self.flags += ["--context", context]
        if namespace:
            self.flags += ["-n", namespace]

    def run(self, *args):
        """kubectl <args> <flags>. Returns (ok, stdout, error text)."""
        try:
            p = subprocess.run(["kubectl", *args, *self.flags],
                               capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return False, "", "kubectl timed out after 60s (deadline exceeded)"
        err = "\n".join(l for l in p.stderr.splitlines() if not re.match(r"^E\d{4} ", l)).strip()
        return p.returncode == 0, p.stdout, err or p.stderr.strip()

    def json(self, *args):
        ok, out, err = self.run(*args, "-o", "json")
        if not ok:
            return None, err
        try:
            return json.loads(out), ""
        except json.JSONDecodeError as e:
            return None, f"unparseable kubectl output: {e}"


def parse_time(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_minutes(s, now):
    t = parse_time(s)
    return None if t is None else int((now - t).total_seconds() // 60)


def one_line(s):
    return " ".join((s or "").split())


def conditions(obj):
    return {c.get("type"): c for c in (obj.get("status") or {}).get("conditions") or []}


def print_conditions(obj):
    for c in (obj.get("status") or {}).get("conditions") or []:
        print(f"    {c.get('type')}={c.get('status')}  {c.get('reason', '')}: {one_line(c.get('message'))}")


def selector_text(sel):
    return json.dumps(sel, separators=(",", ":"), sort_keys=True) if sel else ""


def section(title):
    print()
    print(f"== {title} ==")


def main():
    ap = argparse.ArgumentParser(usage="collect.py <principal-CR-name> [-n <ns>] [--context <ctx>] [--log-lines N]")
    ap.add_argument("name")
    ap.add_argument("-n", "--namespace")
    ap.add_argument("--context")
    ap.add_argument("--log-lines", type=int, default=2000)
    args = ap.parse_args()
    if args.log_lines < 1:
        ap.error("--log-lines needs a positive number")
    if not shutil.which("kubectl"):
        print("error: kubectl not found on PATH", file=sys.stderr)
        return 2

    kube = Kube(args.namespace, args.context)
    now = dt.datetime.now(dt.timezone.utc)
    ok, ctx, _ = kube.run("config", "current-context")
    print(f"COLLECTED: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print(f"CONTEXT:   {ctx.strip() if ok else 'unknown'}")

    # --- 1. Principal ---------------------------------------------------------
    section(f"1. Principal {args.name}")
    p, err = kube.json("get", "principals.kerberator.dell.com", args.name)
    if p is None:
        if UNREACHABLE.search(err):
            # An unreachable cluster is not a missing Principal; don't send the
            # agent looking for another name.
            print(f"UNREACHABLE: the cluster API did not answer ({err.splitlines()[-1] if err else ''})")
            print("HINT: check the kubeconfig context and any tunnel or VPN it needs, then re-run.")
            return 4
        print(f"UNAVAILABLE: {err}")
        print("HINT: Principal CR names are not UIDs or Kerberos names; 'kubectl krb list' shows the real names.")
        # kubectl plugins need their name before any flags.
        ok, out, _ = kube.run("krb", "list")
        for line in out.splitlines():
            print(f"  {line}")
        return 3

    spec, status = p.get("spec") or {}, p.get("status") or {}
    meta = p.get("metadata") or {}
    uid = spec.get("uid")
    kprinc = spec.get("principal") or ""
    preal = kprinc.split("@", 1)[1] if "@" in kprinc else ""
    tenant = (spec.get("tenantRef") or {}).get("name") or ""
    disabled = bool(spec.get("disabled"))
    dreason = spec.get("disabledReason") or ""
    sref = spec.get("keytabSecretRef") or {}
    secret, skey = sref.get("name") or "", sref.get("key") or ""
    kvno = status.get("kvnoFromSecret") or ""
    enctypes = status.get("enctypesFromSecret") or ""
    observed = status.get("keytabObservedAt") or ""
    nodesel = spec.get("nodeSelector") or {}
    ns = meta.get("namespace") or args.namespace or ""
    pready_c = conditions(p).get("Ready") or {}
    pready, preason = pready_c.get("status") or "", pready_c.get("reason") or ""

    # The Secret's existence only: `-o name` prints no data.
    keytab_secret = "none"
    if secret:
        ok, _, err = kube.run("get", "secret", secret, "-o", "name")
        keytab_secret = "present" if ok else ("missing" if "NotFound" in err else "unknown")

    print(f"  uid:              {uid}")
    print(f"  kerberos name:    {kprinc}   (realm: {preal or 'none'})")
    print(f"  namespace:        {ns}")
    print(f"  tenant:           {tenant}")
    print(f"  disabled:         {str(disabled).lower()}{f'  (reason: {dreason})' if dreason else ''}")
    print(f"  keytab secret:    {secret or '<none set>'}{f' (key: {skey})' if skey else ''}"
          f"   exists: {keytab_secret}   (contents never read)")
    print(f"  KVNO in secret:   {kvno or '<not observed>'}   observed: {observed or 'never'}")
    print(f"  enctypes:         {enctypes or '<not observed>'}")
    print(f"  node selector:    {selector_text(nodesel) or '<none: every node the Tenant serves>'}")
    print("  conditions:")
    print_conditions(p)

    # --- 2. Tenant --------------------------------------------------------------
    section(f"2. Tenant {tenant} (realm and KDC configuration)")
    tenant_found, trealm, tdisabled, renew, cachedir = "unknown", "", False, None, ""
    keytabs_resolved = roster_consistent = ""
    t, err = kube.json("get", "tenants.kerberator.dell.com", tenant) if tenant else (None, "no tenantRef set")
    if t is None:
        print(f"UNAVAILABLE: {err}")
        if "NotFound" in err or not tenant:
            tenant_found = "no"
    else:
        tenant_found = "yes"
        tspec = t.get("spec") or {}
        daemon = tspec.get("daemon") or {}
        trealm = tspec.get("realm") or ""
        tdisabled = bool(tspec.get("disabled"))
        renew = daemon.get("renewMinutes")
        cachedir = daemon.get("hostCachePath") or ""
        tconds = conditions(t)
        keytabs_resolved = (tconds.get("KeytabsResolved") or {}).get("status") or ""
        roster_consistent = (tconds.get("RosterConsistent") or {}).get("status") or ""
        print(f"  realm:            {trealm}")
        print(f"  disabled:         {str(tdisabled).lower()}")
        print(f"  renew interval:   {renew if renew else '30 (default)'} minutes")
        print(f"  ticket cache dir: {cachedir or '/tmp (default)'} on each node, file krb5cc_<uid>")
        print(f"  daemon nodes:     {selector_text(daemon.get('nodeSelector')) or '<all nodes>'}")
        print("  conditions:")
        print_conditions(t)
        conf = daemon.get("krb5Conf") or ""
        print("  krb5.conf (realm and KDC lines only):")
        if conf:
            for line in conf.splitlines():
                if KRB5_LINES.search(line):
                    print(f"    {line}")
        else:
            print("    (empty: the daemon image must supply its own krb5.conf)")
    renew_eff = renew if isinstance(renew, int) and renew > 0 else 30

    # --- 3. Per-node status -------------------------------------------------------
    section("3. Per-node status (as the operator reports it)")
    nodes = status.get("nodes") or []
    total = failing = minted = pruned = noreport = daemondown = stale = 0
    nodemsg = ""
    if not nodes:
        print("  (no nodes reported)")
    else:
        print(f"  {'NODE':<12} {'READY':<6} {'REASON':<12} {'LAST-OBSERVED':<22} {'AGE':<8} MESSAGE")
        for n in nodes:
            total += 1
            reason = n.get("reason") or ""
            seen = n.get("lastObserved") or ""
            msg = one_line(n.get("message"))
            age = age_minutes(seen, now)
            if reason == "Minted":
                minted += 1
            elif reason in ("MintFailed", "KeytabMissing"):
                failing += 1
                nodemsg = nodemsg or msg
            elif reason == "Pruned":
                pruned += 1
            elif reason == "":
                noreport += 1          # no recent per-UID report from this node
            else:
                daemondown += 1        # pod-readiness fallback, e.g. ContainersNotReady
            # The daemon re-mints every renew interval; a report older than two of
            # those means this node has stopped reporting for this UID.
            if age is not None and age > 2 * renew_eff:
                stale += 1
            ready = str(bool(n.get("daemonPodReady"))).lower()
            print(f"  {n.get('name', ''):<12} {ready:<6} {reason or '-':<12} {seen or '-':<22} "
                  f"{(str(age) + 'm') if age is not None else '':<8} {msg}")

    # --- 4. Daemon pods -------------------------------------------------------------
    section(f"4. Daemon pods for tenant {tenant}")
    sel = f"app.kubernetes.io/name=kerberator-daemon,app.kubernetes.io/instance={tenant}"
    pods_total = pods_ready = notready_other = notready_starting = 0
    podnames, probe_uids = [], set()
    plist, err = kube.json("get", "pods", "-l", sel)
    if plist is None:
        print(f"UNAVAILABLE: {err}")
    else:
        print(f"  {'POD':<34} {'NODE':<10} {'PHASE':<9} {'READY':<6} RESTARTS")
        for pod in plist.get("items") or []:
            pn = pod["metadata"]["name"]
            cs = ((pod.get("status") or {}).get("containerStatuses") or [{}])[0]
            podready = bool(cs.get("ready"))
            restarts = cs.get("restartCount") or 0
            pods_total += 1
            pods_ready += podready
            podnames.append(pn)
            print(f"  {pn:<34} {(pod.get('spec') or {}).get('nodeName', ''):<10} "
                  f"{(pod.get('status') or {}).get('phase', ''):<9} {str(podready).lower():<6} {restarts}")
            # For a pod that isn't ready or has restarted, show why (scheduling,
            # image pull, crash, readiness probe) from its own Kubernetes events.
            if not podready or restarts:
                ev, _ = kube.json("get", "events", "--field-selector", f"involvedObject.name={pn}")
                items = sorted((ev or {}).get("items") or [],
                               key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "", reverse=True)[:5]
                for e in items:
                    print(f"      [{e.get('lastTimestamp') or e.get('eventTime') or ''}] "
                          f"{e.get('type', '')} {e.get('reason', '')}: {one_line(e.get('message'))}")
                # The readiness probe fails when ANY expected ticket file on the node
                # is missing or empty, so a not-ready pod is often a consequence of a
                # mint failure, not a daemon fault. Record which UIDs it names.
                if not podready:
                    found = {int(m) for e in items for m in READYZ.findall(e.get("message") or "")}
                    started = parse_time(((cs.get("state") or {}).get("running") or {}).get("startedAt")
                                         or (pod.get("status") or {}).get("startTime"))
                    if found:
                        probe_uids |= found
                    elif started and (now - started).total_seconds() < 90:
                        # Just (re)started: the readiness probe hasn't reported yet.
                        notready_starting += 1
                    else:
                        notready_other += 1

    # --- 5. Kerberos events for this UID ------------------------------------------------
    section(f"5. Kerberos events for uid {uid} (newest first, full messages)")
    latest_reason = latest_msg = ""
    # Events from before the operator last read new keytab bytes describe an
    # EARLIER keytab: they say nothing about whether the current key works.
    keytab_read = parse_time(observed)
    since_failed = since_ok = None
    ev, err = kube.json("get", "events")
    if ev is None:
        print(f"UNAVAILABLE: {err}")
    else:
        mine = [e for e in ev.get("items") or []
                if ((e.get("metadata") or {}).get("annotations") or {}).get("kerberator.dell.com/uid") == str(uid)]
        mine.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "", reverse=True)
        if not mine:
            print("  (none: not reported yet, or expired; Kubernetes keeps events for about an hour)")
        if keytab_read:
            since_failed = since_ok = 0
        for i, e in enumerate(mine):
            when = e.get("lastTimestamp") or e.get("eventTime") or ""
            t = parse_time(when)
            older = bool(keytab_read and t and t < keytab_read)
            if keytab_read and not older:
                since_failed += e.get("reason") in ("KerberosMintFailed", "KerberosKeytabMissing")
                since_ok += e.get("reason") == "KerberosMintSucceeded"
            if i < 20:
                tag = "  [before current keytab]" if older else ""
                print(f"  [{when}] {e.get('reason', '')} on {(e.get('involvedObject') or {}).get('name', '')} "
                      f"(x{e.get('count') or 1}){tag}: {one_line(e.get('message'))}")
        if mine:
            latest_reason, latest_msg = mine[0].get("reason") or "", one_line(mine[0].get("message"))

    # --- 6. Daemon logs ------------------------------------------------------------------
    section(f"6. Daemon log lines for {kprinc} (last {args.log_lines} lines per pod)")
    logerr = ""
    for pn in podnames:
        ok, out, err = kube.run("logs", pn, f"--tail={args.log_lines}")
        if not ok:
            print(f"  {pn}: UNAVAILABLE: {err.splitlines()[0] if err else 'no output'}")
            continue
        lines = out.splitlines()
        # The kinit error can wrap onto the next line, so keep one line after each match.
        hits = [i for i, l in enumerate(lines) if kprinc and kprinc in l]
        shown = []
        for i in hits:
            shown += lines[i:i + 2]
        print(f"  --- {pn}")
        if shown:
            for l in shown[-8:]:
                print(f"    {l}")
        else:
            print(f"    (no lines mention {kprinc})")
        # Only a pod whose LATEST line for this principal is an error counts as
        # failing now; an old error followed by a success is already resolved.
        if hits and "ERROR" in lines[hits[-1]]:
            logerr = one_line(" ".join(lines[hits[-1]:hits[-1] + 2]))
    if not podnames:
        print("  (no daemon pods found)")

    # --- 7. Other Principals in the namespace ----------------------------------------------
    section(f"7. All Principals in namespace {ns} (their OWN mint results)")
    # Ready alone is misleading here: one Principal's missing ticket makes the
    # node's daemon pod not ready, which marks every Principal on that Tenant
    # not Ready. Each Principal's own per-node reasons show who is really failing.
    ns_total = ns_bad = ns_mintfail = 0
    dup_uid = "no"
    allp, err = kube.json("get", "principals.kerberator.dell.com")
    if allp is None:
        print(f"UNAVAILABLE: {err}")
    else:
        for o in allp.get("items") or []:
            on = o["metadata"]["name"]
            ospec = o.get("spec") or {}
            oready = (conditions(o).get("Ready") or {}).get("status") or ""
            reasons = [n.get("reason") for n in (o.get("status") or {}).get("nodes") or [] if n.get("reason")]
            ns_total += 1
            ns_bad += oready != "True"
            ns_mintfail += any(r in ("MintFailed", "KeytabMissing") for r in reasons)
            otenant = (ospec.get("tenantRef") or {}).get("name") or ""
            if on != args.name and ospec.get("uid") == uid and otenant == tenant:
                dup_uid = f"yes ({on})"
            print(f"  {on}  uid={ospec.get('uid')}  tenant={otenant}  Ready={oready or 'unknown'}  "
                  f"node results: {' '.join(reasons) or 'none'}")

    # --- 8. Signals -------------------------------------------------------------------------
    section("8. SIGNALS")
    # The current failure text: the newest event if it is a failure, else the
    # daemon logs if a pod's latest line for this principal is an error. Events
    # expire after about an hour and logs may be unreadable; the per-node status
    # keeps the last message, so use it when nothing newer is available.
    if latest_reason in ("KerberosMintFailed", "KerberosKeytabMissing"):
        errtext = latest_msg
    else:
        errtext = logerr
    if not errtext and failing:
        errtext = nodemsg
    signature = classify(errtext)
    if latest_reason == "KerberosKeytabMissing":
        signature = "keytab-missing"
    realm_match = "unknown"
    if trealm and preal:
        realm_match = "yes" if trealm == preal else "no"
    daemon_reason = "none"
    if pods_total and pods_ready < pods_total:
        uids = ",".join(str(u) for u in sorted(probe_uids))
        if uids and not notready_other:
            daemon_reason = f"missing-ticket(uid={uids})"
        elif uids:
            daemon_reason = f"mixed(missing-ticket uid={uids}; other)"
        elif notready_starting and not notready_other:
            daemon_reason = "starting"
        else:
            daemon_reason = "other"

    signals = [
        ("principal_disabled", str(disabled).lower()),
        ("tenant_disabled", str(tdisabled).lower()),
        ("principal_ready", f"{pready or 'unknown'}  ({preason or 'no reason'})"),
        ("tenant_found", tenant_found),
        ("keytab_secret", keytab_secret),
        ("keytabs_resolved", keytabs_resolved or "unknown"),
        ("duplicate_uid", dup_uid),
        ("roster_consistent", roster_consistent or "unknown"),
        ("realm_match", f"{realm_match}            (principal @{preal} vs tenant {trealm})"),
        ("kvno_observed", kvno or "none"),
        ("events_since_keytab_read",
         f"{since_failed} failed, {since_ok} succeeded   (keytab read {observed})" if since_failed is not None
         else "unknown"),
        ("nodes_minted", f"{minted}/{total}"),
        ("nodes_failing", f"{failing}/{total}"),
        ("nodes_no_recent_report", f"{noreport}/{total}"),
        ("nodes_stale_report", f"{stale}/{total}   (older than 2 x renew interval of {renew_eff}m)"),
        ("nodes_pruned", f"{pruned}/{total}"),
        ("nodes_daemon_not_ready", f"{daemondown}/{total}   (no recent report; the daemon pod itself is not ready)"),
        ("daemon_pods_ready", f"{pods_ready}/{pods_total}"),
        ("daemon_not_ready_reason", daemon_reason),
        ("principals_mint_failing_in_namespace", f"{ns_mintfail}/{ns_total}"),
        ("principals_not_ready_in_namespace", f"{ns_bad}/{ns_total}"),
        ("node_selector", selector_text(nodesel) or "none"),
        ("ticket_cache_dir", cachedir or "/tmp"),
        ("latest_event", latest_reason or "none"),
        ("error_signature", signature),
    ]
    for k, v in signals:
        print(f"  {k}={v}")
    if "…" in errtext:
        print("  note=latest event message is truncated; use the daemon log lines in section 6")
    return 0


if __name__ == "__main__":
    sys.exit(main())

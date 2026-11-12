#!/usr/bin/env python3
# diagnose.py — read-only evidence gathering + cause narrowing for a Tenant that is not Ready.
#
#   diagnose.py contexts [--kubeconfig F]
#   diagnose.py tenant <name> [-n NS] [--context C] [--kubeconfig F] [--logs | --no-logs] [--tail N]
#
# Output: line 1 `STATUS: <state>`, line 2 `CONTEXT: <context>`, then optional
# `HINT:` lines, `FINDING: <severity> <code> | <evidence>` lines, `PRIMARY: <code>`,
# `CHECKED-OK: <codes>`, then `---` and labelled raw evidence sections.
# States: healthy | healthy-with-warnings | not-ready | not-found |
#   not-found-in-ns(<ns>[,<ns>...]) | not-found-unverified | forbidden |
#   crd-missing | plugin-missing | error
# Severities: cause (explains not-ready) | warn | info | unknown (could not check).
# Exit codes: 0 healthy*, 4 not-ready, 2 not-found*, 3 forbidden/crd-missing/plugin-missing, 1 error.
#
# Only ever runs: kubectl krb version|describe|list, kubectl get, kubectl logs
# (operator when implicated, failing daemon pods; --logs forces, --no-logs skips
# both), and local `kubectl config` reads. Secrets: key names and sizes only.

import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

USAGE = """\
diagnose.py — read-only evidence gathering + cause narrowing for a Tenant that is not Ready.

  diagnose.py contexts [--kubeconfig F]
  diagnose.py tenant <name> [-n NS] [--context C] [--kubeconfig F] [--logs | --no-logs] [--tail N]

Output: line 1 `STATUS: <state>`, line 2 `CONTEXT: <context>`, then optional
`HINT:` lines, `FINDING: <severity> <code> | <evidence>` lines, `PRIMARY: <code>`,
`CHECKED-OK: <codes>`, then `---` and labelled raw evidence sections.
States: healthy | healthy-with-warnings | not-ready | not-found |
  not-found-in-ns(<ns>[,<ns>...]) | not-found-unverified | forbidden |
  crd-missing | plugin-missing | error
Severities: cause (explains not-ready) | warn | info | unknown (could not check).
Exit codes: 0 healthy*, 4 not-ready, 2 not-found*, 3 forbidden/crd-missing/plugin-missing, 1 error.

Only ever runs: kubectl krb version|describe|list, kubectl get, kubectl logs
(operator when implicated, failing daemon pods; --logs forces, --no-logs skips
both), and local `kubectl config` reads. Secrets: key names and sizes only.
"""


def die_usage():
    sys.stderr.write(USAGE)
    sys.exit(64)


# --- argument parsing (copied from kerberator-inspect/scripts/inspect.py) ---
if len(sys.argv) < 2:
    die_usage()
sub = sys.argv[1]
argv = sys.argv[2:]

common = []      # global kubectl-style flags passed through
kcfg = []        # --kubeconfig F only
ctx_flag = ""
ns_flags = []
logs_mode = "auto"   # auto | on | off
tail_n = 500
pos = []

i = 0
while i < len(argv):
    a = argv[i]
    if a == "--context":
        if i + 1 >= len(argv):
            sys.stderr.write(f"{a} needs a value\n"); sys.exit(64)
        ctx_flag = argv[i + 1]
        common += [a, argv[i + 1]]
        i += 2
    elif a == "--kubeconfig":
        if i + 1 >= len(argv):
            sys.stderr.write(f"{a} needs a value\n"); sys.exit(64)
        kcfg = [a, argv[i + 1]]
        common += [a, argv[i + 1]]
        i += 2
    elif a in ("-n", "--namespace"):
        if i + 1 >= len(argv):
            sys.stderr.write(f"{a} needs a value\n"); sys.exit(64)
        ns_flags += ["-n", argv[i + 1]]
        i += 2
    elif a == "--logs":
        logs_mode = "on"; i += 1
    elif a == "--no-logs":
        logs_mode = "off"; i += 1
    elif a == "--tail":
        if i + 1 >= len(argv):
            sys.stderr.write(f"--tail needs a value\n"); sys.exit(64)
        tail_n = int(argv[i + 1]); i += 2
    elif a in ("-h", "--help"):
        die_usage()
    else:
        pos.append(a); i += 1


# --- helpers copied from inspect.py ---
def krb(*args):
    r = subprocess.run(["kubectl", "krb", *args, *common],
                       capture_output=True, text=True)
    return (r.stdout + r.stderr).rstrip("\n"), r.returncode


def kc(*args):
    r = subprocess.run(["kubectl", *args, *common],
                       capture_output=True, text=True)
    return (r.stdout + r.stderr).rstrip("\n"), r.returncode


def ctx_name():
    if ctx_flag:
        return ctx_flag
    r = subprocess.run(["kubectl", "config", "current-context", *kcfg],
                       capture_output=True, text=True)
    c = r.stdout.strip()
    return c or "<none set>"


def exit_for(status):
    if status in ("ok", "empty", "healthy", "healthy-with-warnings"):
        sys.exit(0)
    if status == "not-ready":
        sys.exit(4)
    if status.startswith("not-found"):
        sys.exit(2)
    if status in ("forbidden", "crd-missing", "plugin-missing"):
        sys.exit(3)
    sys.exit(1)


def emit(status, raw, *hints):
    print(f"STATUS: {status}")
    print(f"CONTEXT: {ctx_name()}")
    for h in hints:
        print(f"HINT: {h}")
    print("---")
    if raw:
        print(raw)
    exit_for(status)


def classify_error(raw):
    if re.search(r"forbidden|cannot (list|get) resource", raw, re.I):
        return "forbidden"
    if re.search(r"no matches for kind|could not find the requested resource|doesn.t have a resource type", raw, re.I):
        return "crd-missing"
    if re.search(r"not found", raw, re.I):
        return "not-found"
    return "error"


# --- pre-flight ---
if not shutil.which("kubectl"):
    emit("plugin-missing", "kubectl is not on PATH",
         "install kubectl, then kubectl-krb (see cli/kubectl-krb/README.md)")

if sub == "contexts":
    r = subprocess.run(["kubectl", "config", "get-contexts", "-o", "name", *kcfg],
                       capture_output=True, text=True)
    names = (r.stdout + r.stderr).rstrip("\n")
    if r.returncode != 0:
        emit("error", names, "could not read kubeconfig")
    n = sum(1 for ln in names.splitlines() if ln) if names else 0
    r2 = subprocess.run(["kubectl", "config", "get-contexts", *kcfg],
                        capture_output=True, text=True)
    table = (r2.stdout + r2.stderr).rstrip("\n")
    r3 = subprocess.run(
        ["kubectl", "config", "view", *kcfg,
         "-o", r'jsonpath={range .clusters[*]}{.name}{"  "}{.cluster.server}{"\n"}{end}'],
        capture_output=True, text=True)
    servers = r3.stdout
    raw = f"{table}\n\nCluster API servers:\n{servers}"
    if n == 0:
        emit("empty", raw, "contexts=0: no cluster is configured; ask the user for a kubeconfig or context")
    elif n == 1:
        emit("ok", raw, "contexts=1: only one cluster is configured; use it and say which")
    else:
        emit("ok", raw, f"contexts={n}: several clusters are configured; if the user did not name one, ask which to inspect")

if sub != "tenant" or len(pos) != 1:
    die_usage()
name = pos[0]

if not shutil.which("jq"):
    emit("plugin-missing", "jq is not on PATH", "install jq (brew install jq / apt install jq)")
_v = subprocess.run(["kubectl", "krb", "version", *common],
                    capture_output=True, text=True)
if _v.returncode != 0:
    emit("plugin-missing", "kubectl krb version failed",
         "build/install the plugin: go build -o kubectl-krb ./cli/kubectl-krb/cmd/kubectl-krb")


# --- not-found explanation (Tenant only) ---
_all = {"out": None, "rc": None}


def load_all():
    if _all["rc"] is not None:
        return
    out, rc = krb("list", "-A")
    _all["out"] = out
    _all["rc"] = rc


# --- findings ---
FINDINGS = []      # list of (severity, code, evidence)
SECTIONS = ""


def add(sev, code, evidence):
    FINDINGS.append((sev, code, evidence))


def has(code):
    return any(c == code for _, c, _ in FINDINGS)


def has_sev(sev):
    return any(s == sev for s, _, _ in FINDINGS)


def section(title, body):
    global SECTIONS
    SECTIONS += f"\n== {title} ==\n{body}\n"


# ---- jq library replacements ----
NOW_EPOCH = int(time.time())


def ts_of(e):
    # (.lastTimestamp // .eventTime // .metadata.creationTimestamp // "1970-01-01T00:00:00Z")
    return (e.get("lastTimestamp")
            or e.get("eventTime")
            or e.get("metadata", {}).get("creationTimestamp")
            or "1970-01-01T00:00:00Z")


def epoch(iso):
    # Strip fractional seconds, normalize +00:00 to Z, parse UTC.
    s = re.sub(r"\.\d+", "", iso).replace("+00:00", "Z")
    # Accept both Z-terminated and non-terminated forms; jq fromdateiso8601 does.
    if s.endswith("Z"):
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    else:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def age_from(iso):
    d = NOW_EPOCH - epoch(iso)
    if d < 60: return f"{d}s"
    if d < 3600: return f"{d // 60}m"
    if d < 86400: return f"{d // 3600}h"
    return f"{d // 86400}d"


def cut(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


def _clean(s):
    # jq: gsub("[\t\n]"; " ")
    return re.sub(r"[\t\n]", " ", s or "")


def _tsv(*fields):
    return "\t".join("" if f is None else str(f) for f in fields)


def _lb(x):
    # lowercase bool like jq
    return "true" if x else "false"


# fetch <prefix> <kubectl get args...>: sets dict entry with out/st.
def fetch(args):
    out, rc = kc("get", *args, "-o", "json")
    if rc == 0:
        return {"out": out, "st": "ok"}
    return {"out": out, "st": classify_error(out)}


# ===================== 1. the Tenant itself =====================
desc, rc = krb("describe", "tenant", name, *ns_flags)
if rc != 0:
    status = classify_error(desc)
    if status == "not-found":
        load_all()
        if _all["rc"] != 0:
            emit("not-found-unverified", desc,
                 f"Tenant '{name}' not found in the queried namespace; could NOT search other namespaces (list -A: {classify_error(_all['out'] or '')})",
                 "absence elsewhere is unknown; try the namespace you expect with -n")
        matches = []
        for line in (_all["out"] or "").splitlines():
            f = line.split()
            if len(f) >= 3 and f[1] == "Tenant" and f[2] == name:
                matches.append(f[0])
        found = ",".join(sorted(set(matches)))
        if found:
            emit(f"not-found-in-ns({found})", desc,
                 f"Tenant '{name}' exists in: {found} (re-run with -n <ns>)")
        emit("not-found", desc,
             f"no Tenant '{name}' in any namespace; run kerberator-inspect 'list -A' to see real names")
    emit(status, desc)

section("kubectl krb describe tenant", desc)

T = fetch(["tenants.kerberator.dell.com", name, *ns_flags])
if T["st"] != "ok":
    emit(T["st"], T["out"], "describe succeeded but 'kubectl get tenant -o json' failed")
T_obj = json.loads(T["out"])
ns = T_obj.get("metadata", {}).get("namespace", "")
nsf = ["-n", ns]
ds = f"{name}-kerberator-daemon"
cm = f"{name}-roster"


def cond(t):
    for c in (T_obj.get("status", {}) or {}).get("conditions", []) or []:
        if c.get("type") == t:
            return f"{c.get('status', '')}|{c.get('reason', '')}|{c.get('message', '') or ''}"
    return "absent"


c_ready = cond("Ready")
c_ds = cond("DaemonSetReady")
c_kt = cond("KeytabsResolved")
c_roster = cond("RosterConsistent")
n_conds = len((T_obj.get("status", {}) or {}).get("conditions", []) or [])
realm = T_obj.get("spec", {}).get("realm", "") or ""
disabled = bool(T_obj.get("spec", {}).get("disabled", False))
deleting = T_obj.get("metadata", {}).get("deletionTimestamp", "") or ""
krb5_spec = T_obj.get("spec", {}).get("daemon", {}).get("krb5Conf", "") or ""
image_spec = T_obj.get("spec", {}).get("daemon", {}).get("image", "") or ""
nodesel_obj = T_obj.get("spec", {}).get("daemon", {}).get("nodeSelector") or {}
nodesel = json.dumps(nodesel_obj, separators=(",", ":"))
renew = int(T_obj.get("spec", {}).get("daemon", {}).get("renewMinutes") or 0)
if renew == 0:
    renew = 30


def _spec_excerpt():
    spec = T_obj.get("spec", {}) or {}
    daemon = spec.get("daemon", {}) or {}
    status = T_obj.get("status", {}) or {}
    rows = []
    rows.append(f'realm:          {spec.get("realm")}')
    rows.append(f'disabled:       {_lb(spec.get("disabled", False))}')
    rows.append(f'deleting since: {deleting or "-"}')
    rows.append(f'daemon.image:   {daemon.get("image") or "<operator default>"}')
    rows.append(f'nodeSelector:   {json.dumps(daemon.get("nodeSelector") or {}, separators=(",", ":"))}')
    rows.append(f'tolerations:    {len(daemon.get("tolerations") or [])}')
    renew_spec = daemon.get("renewMinutes")
    rows.append(f'renewMinutes:   {renew_spec if renew_spec is not None else "<default 30>"}')
    if krb5_spec == "":
        rows.append('krb5Conf:       <empty>')
    else:
        rows.append(f'krb5Conf:       {len(krb5_spec.split(chr(10)))} lines')
    rows.append(f'status:         desired={status.get("desiredNodes") or 0} ready={status.get("readyNodes") or 0} principals={status.get("principalCount") or 0}')
    for c in (status.get("conditions") or []):
        rows.append(f'condition:      {c.get("type")}={c.get("status")} {c.get("reason")} {c.get("message") or ""} (since {c.get("lastTransitionTime") or "?"})')
    return "\n".join(rows)


section("tenant spec/status (excerpt)", _spec_excerpt())

if deleting:
    add("cause", "tenant-deleting",
        f"deletionTimestamp={deleting}; owned DaemonSet/ConfigMap/Secret are being garbage-collected")
if disabled:
    add("cause", "tenant-disabled",
        "spec.disabled=true: roster is intentionally empty, Principals report TenantDisabled (not a fault)")
if n_conds == 0:
    add("cause", "reconcile-not-completing",
        f"status has no conditions: the operator never finished a reconcile of {ns}/{name} (operator down, not watching, or failing before step 8)")

ds_s, ds_r, ds_m = (c_ds.split("|", 2) + ["", "", ""])[:3]
kt_s, kt_r, kt_m = (c_kt.split("|", 2) + ["", "", ""])[:3]
ro_s, ro_r, ro_m = (c_roster.split("|", 2) + ["", "", ""])[:3]
if ro_s == "False":
    add("cause", "duplicate-uid", f"RosterConsistent=False {ro_r}: {ro_m}")
if kt_s == "False":
    add("cause", "keytabs-unresolved", f"KeytabsResolved=False {kt_r}: {kt_m}")
if ds_s == "Unknown":
    add("cause", "daemonset-missing", f"DaemonSetReady=Unknown {ds_r}: operator could not find DaemonSet {ds}")


# ===================== 2. operator =====================
OP = fetch(["deployments", "-A", "-l", "app.kubernetes.io/name=kerberator"])
op_ns = ""
op_name = ""
if OP["st"] == "ok":
    op_obj = json.loads(OP["out"])
    op_lines_list = []
    for d in op_obj.get("items", []):
        md = d.get("metadata", {})
        sp = d.get("spec", {}) or {}
        st = d.get("status", {}) or {}
        avail = "?"
        for c in st.get("conditions", []) or []:
            if c.get("type") == "Available":
                avail = c.get("status", "?")
                break
        op_lines_list.append(_tsv(
            md.get("namespace"), md.get("name"),
            f'{st.get("readyReplicas") or 0}/{sp.get("replicas") or 1}',
            avail,
        ))
    op_lines = "\n".join(op_lines_list)
    section("operator Deployment(s) (label app.kubernetes.io/name=kerberator)",
            op_lines if op_lines else "<none found>")
    if not op_lines:
        add("warn", "operator-not-found",
            "no Deployment labelled app.kubernetes.io/name=kerberator in any namespace (custom install name, or operator not deployed)")
    else:
        first = op_lines_list[0].split("\t")
        op_ns, op_name, op_ready, op_avail = first[0], first[1], first[2], first[3]
        if op_ready.split("/", 1)[0] == "0":
            add("cause", "operator-down",
                f"operator Deployment {op_ns}/{op_name} ready={op_ready} Available={op_avail}")
            pods_out, pods_rc = kc("get", "pods", "-n", op_ns,
                                   "-l", "app.kubernetes.io/name=kerberator",
                                   "-o", "json")
            op_pods = ""
            if pods_rc == 0:
                try:
                    po = json.loads(pods_out)
                    rows = []
                    for p in po.get("items", []):
                        cs = (p.get("status", {}).get("containerStatuses") or [{}])[0] or {}
                        waiting = cs.get("state", {}).get("waiting") or {}
                        rows.append(f'{p.get("metadata", {}).get("name")} {p.get("status", {}).get("phase")} {waiting.get("reason") or "-"} {cut(waiting.get("message") or "", 120)}')
                    op_pods = "\n".join(rows)
                except Exception:
                    op_pods = ""
            section("operator pods", op_pods)
else:
    add("unknown", "operator-health",
        f"could not list operator Deployments cluster-wide ({OP['st']})")


# ===================== 3. DaemonSet + pods + events =====================
DS = fetch(["daemonset", ds, *nsf])
desired = None
ready = None
updated = None
missched = None
ds_image = ""
if DS["st"] == "ok":
    ds_obj = json.loads(DS["out"])
    ds_status = ds_obj.get("status", {}) or {}
    desired = ds_status.get("desiredNumberScheduled") or 0
    ready = ds_status.get("numberReady") or 0
    updated = ds_status.get("updatedNumberScheduled") or 0
    missched = ds_status.get("numberMisscheduled") or 0
    ds_image = ds_obj["spec"]["template"]["spec"]["containers"][0]["image"]
    section(f"DaemonSet {ns}/{ds}",
            f"desired={desired} ready={ready} updated={updated} misscheduled={missched} image={ds_image}")
    if updated < desired:
        add("warn", "rollout-in-progress",
            f"updated={updated} of desired={desired}: a rollout is in progress or stuck")
    if image_spec and ds_image != image_spec:
        add("warn", "config-not-synced",
            f"spec.daemon.image={image_spec} but DaemonSet runs {ds_image}: operator has not applied the latest spec")
elif DS["st"] == "not-found":
    if not has("daemonset-missing"):
        add("cause", "daemonset-missing", f"DaemonSet {ns}/{ds} does not exist")
else:
    add("unknown", "daemonset", f"could not read DaemonSet {ns}/{ds} ({DS['st']})")


def _toleration_matches(tol, taint):
    t_eff = tol.get("effect") or ""
    if not (t_eff == "" or t_eff == taint.get("effect")):
        return False
    op = tol.get("operator") or "Equal"
    if op == "Exists":
        return (tol.get("key") or "") == "" or tol.get("key") == taint.get("key")
    return tol.get("key") == taint.get("key") and (tol.get("value") or "") == (taint.get("value") or "")


def _node_rows(nodes_items, sel, tols):
    rows = []
    for n in nodes_items:
        md = n.get("metadata", {}) or {}
        labels = md.get("labels") or {}
        match = all(labels.get(k) == v for k, v in sel.items())
        blocking = []
        for t in (n.get("spec", {}) or {}).get("taints", []) or []:
            if t.get("effect") not in ("NoSchedule", "NoExecute"):
                continue
            tolerated = any(_toleration_matches(tol, t) for tol in tols)
            if not tolerated:
                blocking.append(f'{t.get("key")}={t.get("value") or ""}:{t["effect"]}')
        unschedulable = _lb((n.get("spec", {}) or {}).get("unschedulable", False))
        rows.append(_tsv(
            md.get("name"),
            f"selector-match={_lb(match)}",
            f"unschedulable={unschedulable}",
            f"untolerated-taints={','.join(blocking)}",
        ))
    return "\n".join(rows)


NODES = {"st": None, "out": None}
node_rows = ""
n_total = 0
n_match = 0
n_fit = 0
if DS["st"] == "ok" and (desired == 0 or nodesel != "{}"):
    NODES = fetch(["nodes"])
    if NODES["st"] == "ok":
        nodes_obj = json.loads(NODES["out"])
        tols = (ds_obj.get("spec", {}).get("template", {}).get("spec", {}) or {}).get("tolerations") or []
        node_rows = _node_rows(nodes_obj.get("items", []), nodesel_obj, tols)
        section(f"nodes vs nodeSelector {nodesel}", node_rows)
        rows = [r for r in node_rows.splitlines() if r]
        n_total = len(rows)
        n_match = sum(1 for r in rows if r.split("\t")[1] == "selector-match=true")
        n_fit = sum(1 for r in rows if (
            r.split("\t")[1] == "selector-match=true"
            and r.split("\t")[2] == "unschedulable=false"
            and r.split("\t")[3] == "untolerated-taints="
        ))
    elif desired == 0:
        add("unknown", "nodes", f"could not list nodes to check the nodeSelector ({NODES['st']})")

if DS["st"] == "ok" and desired == 0 and not disabled:
    if NODES["st"] == "ok" and n_match > 0 and n_fit == 0:
        parts = []
        for r in node_rows.splitlines():
            fields = r.split("\t")
            if len(fields) < 4 or fields[1] != "selector-match=true":
                continue
            p = fields[0]
            if fields[2] == "unschedulable=true":
                p += " unschedulable"
            if fields[3] != "untolerated-taints=":
                p += " tainted " + fields[3][len("untolerated-taints="):]
            parts.append(p)
        blocked = ";".join(parts)
        add("cause", "no-eligible-nodes",
            f"DaemonSet desiredNumberScheduled=0: {n_match} node(s) match nodeSelector {nodesel} but none accept the daemon ({blocked}): add spec.daemon.tolerations for those taints, or uncordon")
    elif NODES["st"] == "ok" and n_match == 0:
        add("cause", "no-eligible-nodes",
            f"DaemonSet desiredNumberScheduled=0: nodeSelector {nodesel} matches none of {n_total} node(s)")
    else:
        add("cause", "no-eligible-nodes",
            f"DaemonSet desiredNumberScheduled=0: nodeSelector {nodesel} / tolerations match no schedulable node")
elif DS["st"] == "ok" and nodesel != "{}" and NODES["st"] == "ok" and n_total > 0:
    add("info", "daemon-node-subset",
        f"spec.daemon.nodeSelector {nodesel} matches {n_match} of {n_total} node(s); the daemon runs only there by design, and ready {ready}/{desired} counts matching nodes only")


PODS = fetch(["pods", *nsf,
              "-l", f"app.kubernetes.io/name=kerberator-daemon,app.kubernetes.io/instance={name}"])
pod_rows_list = []
pod_rows = ""
if PODS["st"] == "ok":
    pods_obj = json.loads(PODS["out"])
    for p in pods_obj.get("items", []):
        md = p.get("metadata", {}) or {}
        sp = p.get("spec", {}) or {}
        st = p.get("status", {}) or {}
        cs_list = st.get("containerStatuses") or []
        cs = cs_list[0] if cs_list else {}
        sched = None
        for c in st.get("conditions", []) or []:
            if c.get("type") == "PodScheduled" and c.get("status") == "False":
                sched = c; break
        pname = md.get("name")
        node = sp.get("nodeName") or "<unscheduled>"
        if md.get("deletionTimestamp"):
            phase = "Terminating"
        else:
            phase = st.get("phase")
        pready = "-"
        for c in st.get("conditions", []) or []:
            if c.get("type") == "Ready":
                pready = c.get("status", "-"); break
        waiting = (cs.get("state") or {}).get("waiting") or {}
        wreason = waiting.get("reason") or "-"
        restarts = str(cs.get("restartCount") or 0)
        term = (cs.get("lastState") or {}).get("terminated")
        if term:
            lastterm = f'{term.get("reason") or "?"}(exit {term.get("exitCode")})'
        else:
            lastterm = "-"
        msg = cut(_clean(waiting.get("message") or (sched.get("message") if sched else "") or ""), 160)
        row = _tsv(pname, node, phase, pready, wreason, restarts, lastterm, msg)
        pod_rows_list.append((pname, node, phase, pready, wreason, restarts, lastterm, msg))
        pod_rows += row + "\n"
    pod_rows = pod_rows.rstrip("\n")
    section("daemon pods (NAME NODE PHASE READY WAITING RESTARTS LAST-TERMINATED MESSAGE)",
            pod_rows or "<none>")
else:
    add("unknown", "pods", f"could not list daemon pods ({PODS['st']})")


EV = fetch(["events", *nsf])
k8s_ev_rows = []       # list of tsv strings
d_events = []          # list of dicts {pod, reason, message, uid, ts}
d_latest_list = []     # list of (pod, uid, reason, age, message)
if EV["st"] == "ok":
    ev_obj = json.loads(EV["out"])
    # Kubernetes events on DaemonSet/pods, newest first (excluding Kerberos*).
    k_items = []
    for e in ev_obj.get("items", []):
        obj = e.get("involvedObject", {}) or {}
        on = obj.get("name") or ""
        if not (on == ds or on.startswith(ds + "-")):
            continue
        reason = e.get("reason", "") or ""
        if reason.startswith("Kerberos"):
            continue
        k_items.append(e)
    k_items.sort(key=lambda e: epoch(ts_of(e)), reverse=True)
    for e in k_items:
        obj = e.get("involvedObject", {}) or {}
        k8s_ev_rows.append(_tsv(
            age_from(ts_of(e)),
            e.get("type"),
            e.get("reason"),
            f'{obj.get("kind")}/{obj.get("name")}',
            f'x{e.get("count") or 1}',
            cut(_clean(e.get("message") or ""), 200),
        ))
    k8s_ev = "\n".join(k8s_ev_rows)
    section("Kubernetes events on DaemonSet/pods (AGE TYPE REASON OBJECT COUNT MESSAGE), newest first",
            k8s_ev or "(none — never emitted, or expired; the API server keeps events ~1h)")

    # Daemon per-UID Kerberos* events on this tenant's pods.
    for e in ev_obj.get("items", []):
        reason = e.get("reason", "") or ""
        if not reason.startswith("Kerberos"):
            continue
        obj = e.get("involvedObject", {}) or {}
        pn = obj.get("name") or ""
        if not pn.startswith(ds + "-"):
            continue
        msg = e.get("message") or ""
        uid = (e.get("metadata", {}).get("annotations", {}) or {}).get("kerberator.dell.com/uid")
        if not uid:
            m = re.search(r"uid=([0-9]+)", msg)
            uid = m.group(1) if m else "-"
        d_events.append({
            "pod": pn,
            "reason": reason,
            "message": msg,
            "uid": uid,
            "ts": (e.get("lastTimestamp")
                   or e.get("eventTime")
                   or e.get("metadata", {}).get("creationTimestamp")),
        })
    d_events_sorted = sorted(d_events, key=lambda e: epoch(e["ts"]), reverse=True)
    d_ev_rows = "\n".join(
        _tsv(age_from(e["ts"]), e["pod"], f'uid={e["uid"]}', e["reason"],
             cut(_clean(e["message"]), 200))
        for e in d_events_sorted
    )
    section("daemon Kerberos* events (AGE POD UID REASON MESSAGE), newest first",
            d_ev_rows or "(none — daemons have not reported yet, or events expired/disabled)")

    # Latest event per (pod, uid). Scope to live pods if we have pod data.
    live_set = None
    if PODS["st"] == "ok":
        live_set = {p[0] for p in pod_rows_list}  # pod names
    scoped = [e for e in d_events if live_set is None or e["pod"] in live_set]
    groups = {}
    for e in scoped:
        key = (e["pod"], e["uid"])
        if key not in groups or epoch(e["ts"]) > epoch(groups[key]["ts"]):
            groups[key] = e
    for e in groups.values():
        d_latest_list.append((e["pod"], e["uid"], e["reason"], age_from(e["ts"]),
                              cut(_clean(e["message"]), 200)))
else:
    add("unknown", "events", f"could not list events in {ns} ({EV['st']})")


def first_ev(reason_re):
    # Return "age reason object: message" for the newest matching k8s event.
    for row in k8s_ev_rows:
        f = row.split("\t")
        if re.search(reason_re, f[2]):
            return f"{f[0]} {f[2]} {f[3]}: {f[5]}"
    return ""


not_ready_pods = 0
log_pods = []    # list of entries "<pod>" or "<pod>:previous"

for (p, node, phase, pready, waiting, restarts, lastterm, msg) in pod_rows_list:
    if not p:
        continue
    if pready == "True":
        continue
    if phase == "Terminating":
        continue
    not_ready_pods += 1
    if waiting in ("ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"):
        add("cause", "pod-image-pull", f"{p} on {node}: {waiting}: {msg}")
    elif waiting == "CrashLoopBackOff":
        add("cause", "pod-crash-loop",
            f"{p} on {node}: CrashLoopBackOff, restarts={restarts}, last={lastterm} (read 'kubectl logs {p} -n {ns} --previous')")
    elif waiting in ("CreateContainerConfigError", "CreateContainerError"):
        add("cause", "pod-config-error", f"{p} on {node}: {waiting}: {msg}")
    elif waiting == "ContainerCreating":
        e = first_ev(r"FailedMount|FailedAttachVolume")
        if e:
            add("cause", "pod-mount-failed", f"{p} on {node}: {e}")
        else:
            add("warn", "pod-starting", f"{p} on {node}: ContainerCreating (no FailedMount event seen)")
    else:
        if phase == "Pending":
            e = first_ev(r"FailedScheduling")
            suffix = ""
            if msg: suffix += f": {msg}"
            if e: suffix += f"; event: {e}"
            add("cause", "pod-unschedulable", f"{p} ({node}): Pending{suffix}")
        elif phase == "Running":
            e = first_ev(r"Unhealthy")
            extra = ""
            if e: extra += f"; event: {e}"
            if lastterm and lastterm != "-": extra += f"; last terminated {lastterm}"
            add("cause", "pod-not-ready", f"{p} on {node}: Running but Ready={pready}{extra}")
        else:
            add("cause", "pod-not-ready", f"{p} on {node}: phase={phase} Ready={pready} waiting={waiting}")

# FailedCreate counts only while the DaemonSet is still under its desired count.
if DS["st"] == "ok":
    ds_status = (json.loads(DS["out"]).get("status") or {})
    cur = ds_status.get("currentNumberScheduled") or 0
    des = ds_status.get("desiredNumberScheduled") or 0
    if cur < des:
        e = first_ev(r"FailedCreate")
        if e:
            add("cause", "pod-create-failed",
                f"DaemonSet cannot create pods: {e} (PodSecurity admission, quota, or missing ServiceAccount)")

if (DS["st"] == "ok" and desired is not None and desired > 0
        and ready is not None and ready < desired
        and not_ready_pods == 0 and PODS["st"] == "ok"):
    add("cause", "pods-not-ready",
        f"DaemonSet ready={ready}/{desired} but no not-ready pod was listed (pods may be terminating or replaced)")

# Daemon-side causes: latest event per (pod, uid).
for (p, uid, reason, age, msg) in d_latest_list:
    if not p:
        continue
    if reason == "KerberosMintFailed":
        add("cause", "mint-failed", f"uid={uid} on {p}, {age} ago: {msg}")
        log_pods.append(p)
    elif reason == "KerberosKeytabMissing":
        add("cause", "keytab-missing-on-node", f"uid={uid} on {p}, {age} ago: {msg}")
        log_pods.append(p)

# Add crash-loop pods as :previous logs.
for (s, c, ev) in FINDINGS:
    if c == "pod-crash-loop":
        pod = ev.split(" ", 1)[0]
        log_pods.append(f"{pod}:previous")


# principal_status_stale: return explanation string or None.
def principal_status_stale(pname, uid):
    # Preconditions from tenant spec.
    spec = T_obj.get("spec", {}) or {}
    daemon = spec.get("daemon", {}) or {}
    if (daemon.get("renewMinutes") or 0) != 0:
        return None
    if spec.get("eventStalenessThreshold") is not None:
        return None
    if PODS["st"] != "ok" or EV["st"] != "ok" or not_ready_pods != 0:
        return None
    pr_obj = json.loads(PR["out"]) if PR["st"] == "ok" else None
    if not pr_obj:
        return None
    pods_items = json.loads(PODS["out"]).get("items", [])
    # Find the principal.
    principal = None
    for it in pr_obj.get("items", []):
        if it.get("metadata", {}).get("name") == pname:
            principal = it; break
    if not principal:
        return None
    blank = [n.get("name") for n in (principal.get("status", {}).get("nodes") or [])
             if (n.get("reason") or "") == ""]
    if not blank:
        return None
    ok_rows = []
    for n_name in blank:
        pod = None
        for p in pods_items:
            if p.get("spec", {}).get("nodeName") == n_name:
                pod = p.get("metadata", {}).get("name"); break
        if not pod:
            return None
        cand = [e for e in d_events if e["pod"] == pod and e["uid"] == str(uid)]
        if not cand:
            return None
        last = max(cand, key=lambda e: epoch(e["ts"]))
        if last["reason"] != "KerberosMintSucceeded":
            return None
        if NOW_EPOCH - epoch(last["ts"]) <= 300:
            return None
        ok_rows.append(f'{n_name} (last mint {age_from(last["ts"])} ago)')
    if len(ok_rows) != len(blank):
        return None
    return f'every daemon pod is Ready and the last Event on each unreported node is a successful mint: {", ".join(ok_rows)}'


# ===================== 4. Principals + keytab Secrets =====================
PR = fetch(["principals.kerberator.dell.com", *nsf])
pr_rows_list = []
if PR["st"] == "ok":
    pr_obj = json.loads(PR["out"])
    for it in pr_obj.get("items", []):
        if (it.get("spec", {}).get("tenantRef", {}) or {}).get("name") != name:
            continue
        md = it.get("metadata", {}) or {}
        sp = it.get("spec", {}) or {}
        ready_c = None
        for c in (it.get("status", {}).get("conditions") or []):
            if c.get("type") == "Ready":
                ready_c = c; break
        kref = sp.get("keytabSecretRef") or {}
        pr_rows_list.append((
            md.get("name"),
            str(sp.get("uid", "")),
            kref.get("name") or "<none>",
            kref.get("key") or "<none>",
            _lb(sp.get("disabled", False)),
            (ready_c.get("status") if ready_c else "-") or "-",
            (ready_c.get("reason") if ready_c else "-") or "-",
            cut((ready_c.get("message") if ready_c else "") or "", 120),
            "deleting" if md.get("deletionTimestamp") else "-",
        ))
    pr_rows = "\n".join("\t".join(r) for r in pr_rows_list)
    section(f"Principals referencing {name} (NAME UID SECRET KEY DISABLED READY REASON MESSAGE DELETING)",
            pr_rows or "<none>")
    if not pr_rows_list:
        add("info", "no-principals",
            f"no Principal references Tenant {name}: daemons run but have nothing to mint")

    secret_report_lines = []
    checked_secrets = set()
    for (pn, uid, sname, skey, pdis, pready, preason, pmsg, pdel) in pr_rows_list:
        if not pn:
            continue
        if preason == "NoEligibleNodes":
            psel_obj = {}
            for it in pr_obj.get("items", []):
                if it.get("metadata", {}).get("name") == pn:
                    psel_obj = it.get("spec", {}).get("nodeSelector") or {}
                    break
            psel = json.dumps(psel_obj, separators=(",", ":"))
            add("warn", "principal-no-eligible-nodes",
                f"{pn} (uid {uid}): spec.nodeSelector {psel} matches no node, so it is minted nowhere; check node labels (kubectl krb node-labels)")
        elif pready == "False" and preason == "SomeDaemonsNotReady":
            stale = principal_status_stale(pn, uid)
            if stale:
                add("info", "principal-status-stale",
                    f"{pn} (uid {uid}): Ready=False {preason}, but {stale}. Status only, not a fault: with renewMinutes unset the operator trusts a mint for 5 minutes while the daemon renews every 30 (dell/kerberator#22)")
            else:
                if pready != "True" and preason not in ("Disabled", "TenantDisabled", "-"):
                    suf = f": {pmsg}" if pmsg else ""
                    add("warn", "principal-not-ready",
                        f"{pn} (uid {uid}): Ready={pready} {preason}{suf}")
        elif pready != "True" and preason not in ("Disabled", "TenantDisabled", "-"):
            suf = f": {pmsg}" if pmsg else ""
            add("warn", "principal-not-ready",
                f"{pn} (uid {uid}): Ready={pready} {preason}{suf}")

        if sname == "<none>":
            add("cause", "keytab-ref-missing", f"{pn} (uid {uid}) has no spec.keytabSecretRef")
            continue
        # Key names + base64 sizes only; values never leave kubectl.
        keys, rc = kc("get", "secret", sname, *nsf,
                      "-o", r'go-template={{range $k, $v := .data}}{{$k}} {{len $v}}{{"\n"}}{{end}}')
        if rc != 0:
            st = classify_error(keys)
            if st == "not-found":
                add("cause", "keytab-secret-missing",
                    f"{pn} (uid {uid}): Secret {ns}/{sname} does not exist")
            else:
                add("unknown", "keytab-secret",
                    f"{pn}: could not read Secret {ns}/{sname} key names ({st})")
            secret_report_lines.append(f"{sname}: <{st}>")
            continue
        size = ""
        key_rows = [ln for ln in keys.splitlines() if ln]
        for ln in key_rows:
            parts = ln.split()
            if len(parts) >= 2 and parts[0] == skey:
                size = parts[1]
        if size == "":
            all_keys = ",".join(ln.split()[0] for ln in key_rows if ln.strip())
            add("cause", "keytab-key-missing",
                f"{pn} (uid {uid}): Secret {sname} has no key '{skey}' (keys: {all_keys})")
        elif size == "0":
            add("cause", "keytab-empty",
                f"{pn} (uid {uid}): Secret {sname} key '{skey}' is empty")
        if sname not in checked_secrets:
            parts = " ".join(f"{ln.split()[0]}({ln.split()[1]}b64)" for ln in key_rows if len(ln.split()) >= 2)
            secret_report_lines.append(f"{sname}: {parts}")
            checked_secrets.add(sname)
    secret_report = "\n".join(secret_report_lines)
    section("keytab Secrets (key names and base64 sizes only)",
            secret_report or "<none referenced>")
else:
    PR_obj = None
    add("unknown", "principals", f"could not list Principals in {ns} ({PR['st']})")


# ===================== 5. configuration: krb5.conf / roster =====================
if not krb5_spec:
    if has("mint-failed"):
        add("cause", "krb5conf-empty",
            "spec.daemon.krb5Conf is empty and mints are failing: kinit in the daemon has no KDC/realm config (unless baked into a custom image)")
    else:
        add("warn", "krb5conf-empty",
            "spec.daemon.krb5Conf is empty: only works if the daemon image ships its own krb5.conf")
else:
    m = re.search(r"^[ \t]*default_realm[ \t]*=[ \t]*([^ \t\n]+)", krb5_spec, re.M)
    dr = m.group(1) if m else ""
    if dr and dr != realm:
        add("warn", "realm-mismatch",
            f"krb5Conf default_realm={dr} but spec.realm={realm}")
    if realm and realm not in krb5_spec:
        add("warn", "realm-not-in-krb5conf",
            f"spec.realm {realm} does not appear anywhere in krb5Conf ([realms] section missing?)")

cm_krb5 = ""
CM = fetch(["configmap", cm, *nsf])
if CM["st"] == "ok":
    cm_obj = json.loads(CM["out"])
    data = cm_obj.get("data") or {}
    roster_lines = [ln for ln in (data.get("users.roster") or "").split("\n") if ln]
    section(f"roster ConfigMap {ns}/{cm}",
            f"keys: {', '.join(sorted(data.keys()))}\nusers.roster lines: {len(roster_lines)}")
    cm_krb5 = data.get("krb5.conf") or ""
    if cm_krb5 != krb5_spec:
        add("warn", "config-not-synced",
            "roster ConfigMap krb5.conf differs from spec.daemon.krb5Conf: operator has not applied the latest spec")
elif CM["st"] == "not-found":
    if n_conds != 0:
        add("warn", "roster-cm-missing", f"ConfigMap {ns}/{cm} does not exist")
else:
    add("unknown", "roster-cm", f"could not read ConfigMap {ns}/{cm} ({CM['st']})")


# ===================== 5b. the KDC (in-cluster Services only) =====================
kdc_source = cm_krb5 or krb5_spec
kdc = ""
m = re.search(r"^[ \t]*kdc[ \t]*=[ \t]*([^ \t\n]+)", kdc_source, re.M)
if m:
    kdc = m.group(1)

if kdc:
    khost = kdc.split(":", 1)[0]
    ksvc = ""
    kns = ""
    definite = False
    m = re.match(r"^([a-z0-9-]+)\.([a-z0-9-]+)\.svc(\..+)?$", khost)
    if m:
        ksvc = m.group(1); kns = m.group(2); definite = True
    elif re.match(r"^([a-z0-9-]+)\.([a-z0-9-]+)$", khost):
        mm = re.match(r"^([a-z0-9-]+)\.([a-z0-9-]+)$", khost)
        ksvc = mm.group(1); kns = mm.group(2)
    elif re.match(r"^[a-z0-9-]+$", khost):
        ksvc = khost; kns = ns
    kdc_sev = "cause" if has("mint-failed") else "warn"
    if not ksvc:
        add("info", "kdc-external",
            f"KDC {kdc} is outside the cluster; its health was not checked (test from a node, e.g. nc -vz <host> 88)")
    else:
        KSVC = fetch(["service", ksvc, "-n", kns])
        if KSVC["st"] == "ok":
            ksvc_obj = json.loads(KSVC["out"])
            sel_map = (ksvc_obj.get("spec", {}) or {}).get("selector") or {}
            sel = ",".join(f"{k}={v}" for k, v in sel_map.items())
            KEPS = fetch(["endpointslices", "-n", kns,
                          "-l", f"kubernetes.io/service-name={ksvc}"])
            ready_eps = "?"
            if KEPS["st"] == "ok":
                eps_obj = json.loads(KEPS["out"])
                cnt = 0
                for item in eps_obj.get("items", []):
                    for e in item.get("endpoints") or []:
                        if (e.get("conditions") or {}).get("ready") is not False:
                            cnt += 1
                ready_eps = str(cnt)
            kpods = ""
            kstart = ""
            if sel:
                KP = fetch(["pods", "-n", kns, "-l", sel])
                if KP["st"] == "ok":
                    kp_obj = json.loads(KP["out"])
                    rows = []
                    starts = []
                    for p in kp_obj.get("items", []):
                        cs = (p.get("status", {}).get("containerStatuses") or [{}])[0] or {}
                        rp = "-"
                        for c in p.get("status", {}).get("conditions", []) or []:
                            if c.get("type") == "Ready":
                                rp = c.get("status", "-"); break
                        started = ((cs.get("state") or {}).get("running") or {}).get("startedAt") or "-"
                        if started and started != "-":
                            starts.append(started)
                        rows.append(_tsv(p.get("metadata", {}).get("name"), rp,
                                         str(cs.get("restartCount") or 0), started))
                    kpods = "\n".join(rows)
                    kstart = max(starts) if starts else ""
            section(f"KDC {kdc} -> Service {kns}/{ksvc}, ready endpoints: {ready_eps} (POD READY RESTARTS STARTED)",
                    kpods or f"<no pods found for selector '{sel}'>")
            if ready_eps == "?":
                add("unknown", "kdc-endpoints",
                    f"could not read EndpointSlices for {kns}/{ksvc} ({KEPS['st']})")
            elif ready_eps == "0":
                add(kdc_sev, "kdc-not-ready",
                    f"Service {kns}/{ksvc} (the KDC in krb5.conf) has no ready endpoints: the KDC is not serving")
            elif has("mint-failed") and kstart:
                # Find latest mint failure.
                groups = {}
                for e in d_events:
                    key = (e["pod"], e["uid"])
                    if key not in groups or epoch(e["ts"]) > epoch(groups[key]["ts"]):
                        groups[key] = e
                failures = [e for e in groups.values() if e["reason"] == "KerberosMintFailed"]
                if failures:
                    latest = max(failures, key=lambda e: epoch(e["ts"]))
                    fts = latest["ts"]
                    fpod = latest["pod"]
                    if epoch(kstart) > epoch(fts):
                        nxt = ""
                        if PODS["st"] == "ok":
                            for p in json.loads(PODS["out"]).get("items", []):
                                if p.get("metadata", {}).get("name") == fpod:
                                    s2 = (((p.get("status", {}).get("containerStatuses") or [{}])[0] or {}).get("state") or {}).get("running", {}).get("startedAt")
                                    if s2:
                                        s_ep = epoch(s2)
                                        interval = renew * 60
                                        ticks = ((NOW_EPOCH - s_ep) // interval) + 1
                                        nxt_ep = s_ep + ticks * interval
                                        nxt = datetime.fromtimestamp(nxt_ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                                    break
                        suffix = f" (next around {nxt}, later if the node or VM was suspended)" if nxt else ""
                        add("warn", "mint-failure-stale",
                            f"the KDC is serving now and (re)started at {kstart}, after the last mint failure at {fts} on {fpod}: the failure is likely transient. The daemon retries only every {renew}m of node running time{suffix}; restarting the daemon retries now")
        elif KSVC["st"] == "not-found":
            if definite:
                add(kdc_sev, "kdc-service-missing",
                    f"krb5.conf names KDC {kdc} but Service {kns}/{ksvc} does not exist")
            else:
                add("info", "kdc-external",
                    f"KDC {kdc} is not an in-cluster Service; its health was not checked")
        else:
            add("unknown", "kdc", f"could not read KDC Service {kns}/{ksvc} ({KSVC['st']})")


# ===================== 6. operator logs =====================
want_logs = False
if logs_mode == "on":
    want_logs = True
elif logs_mode == "auto":
    if (has("reconcile-not-completing") or has("operator-down")
            or has("config-not-synced") or has("daemonset-missing")):
        want_logs = True

if want_logs:
    if not op_name:
        add("unknown", "operator-logs",
            "operator Deployment not located, so its logs were not read")
    else:
        logs, rc = kc("logs", f"deploy/{op_name}", "-n", op_ns, f"--tail={tail_n}")
        if rc != 0:
            add("unknown", "operator-logs",
                f"could not read logs of {op_ns}/{op_name} ({classify_error(logs)})")
        else:
            mine_lines = [ln for ln in logs.splitlines() if f"{ns}/{name}" in ln][-20:]
            mine_lines = [ln[:400] for ln in mine_lines]
            mine = "\n".join(mine_lines)
            section(f"operator logs: last {tail_n} lines of {op_ns}/{op_name}, filtered to \"{ns}/{name}\" (last 20)",
                    mine if mine else f"<no lines mention {ns}/{name}>")
            err = ""
            for ln in mine_lines:
                if re.search(r'"level":"error"| ERROR |level=error', ln, re.I):
                    err = ln[:300]
            if err:
                add("cause", "operator-reconcile-error",
                    f"latest operator error for {ns}/{name}: {err}")

# Daemon logs: up to 3 pods, last 30 lines each.
if logs_mode != "off" and log_pods:
    seen = set()
    n_count = 0
    for entry in log_pods:
        pname2 = entry.split(":", 1)[0]
        prev = ["--previous"] if entry.endswith(":previous") else []
        if pname2 in seen:
            continue
        seen.add(pname2)
        n_count += 1
        if n_count > 3:
            break
        dl, rc = kc("logs", pname2, "-n", ns, "--tail=30", *prev)
        if rc != 0:
            add("unknown", "daemon-logs",
                f"could not read logs of {ns}/{pname2} ({classify_error(dl)})")
        else:
            label = f"daemon logs: {ns}/{pname2}"
            if prev:
                label += " (previous container)"
            label += ", last 30 lines"
            body = "\n".join(ln[:300] for ln in dl.splitlines())
            section(label, body)


# ===================== verdict =====================
PRECEDENCE = """operator-down operator-reconcile-error reconcile-not-completing tenant-deleting tenant-disabled
daemonset-missing no-eligible-nodes pod-create-failed pod-unschedulable pod-image-pull pod-config-error
pod-mount-failed pod-crash-loop keytab-ref-missing keytab-secret-missing keytab-key-missing keytab-empty
keytabs-unresolved duplicate-uid keytab-missing-on-node kdc-service-missing kdc-not-ready krb5conf-empty mint-failed
pod-not-ready pods-not-ready""".split()

primary = ""
for c in PRECEDENCE:
    for sev, code, _ in FINDINGS:
        if sev == "cause" and code == c:
            primary = c; break
    if primary:
        break


ALL_CHECKS = """operator-down reconcile-not-completing tenant-deleting tenant-disabled daemonset-missing no-eligible-nodes
pod-create-failed pod-unschedulable pod-image-pull pod-config-error pod-mount-failed pod-crash-loop pod-not-ready
keytabs-unresolved keytab-secret-missing keytab-key-missing duplicate-uid mint-failed keytab-missing-on-node
krb5conf-empty realm-mismatch config-not-synced kdc-service-missing kdc-not-ready""".split()


def needs(c):
    if c == "operator-down": return ["operator-health"]
    if c in ("daemonset-missing", "no-eligible-nodes"): return ["daemonset"]
    if c == "pod-create-failed": return ["daemonset", "events"]
    if c.startswith("pod-"): return ["pods"]
    if c in ("keytab-secret-missing", "keytab-key-missing"): return ["principals", "keytab-secret"]
    if c in ("mint-failed", "keytab-missing-on-node"): return ["events"]
    if c == "config-not-synced": return ["roster-cm", "daemonset"]
    if c in ("kdc-service-missing", "kdc-not-ready"): return ["kdc", "kdc-endpoints"]
    return []


ok_list = []
for c in ALL_CHECKS:
    if has(c):
        continue
    verified = True
    for a in needs(c):
        for sev, code, _ in FINDINGS:
            if sev == "unknown" and code == a:
                verified = False; break
        if not verified:
            break
    if verified:
        ok_list.append(c)


if any(sev == "cause" for sev, _, _ in FINDINGS):
    status = "not-ready"
elif (ds_s == "True" and desired is not None and desired > 0
      and ready == desired):
    if any(sev in ("warn", "unknown") for sev, _, _ in FINDINGS):
        status = "healthy-with-warnings"
    else:
        status = "healthy"
else:
    status = "not-ready"
    add("cause", "unexplained",
        f"Tenant not fully ready (DaemonSetReady={ds_s}, nodes {ready if ready is not None else '?'}/{desired if desired is not None else '?'}) but no specific cause matched; see raw evidence")
    primary = "unexplained"


print(f"STATUS: {status}")
print(f"CONTEXT: {ctx_name()}")
if c_ready.startswith("True|"):
    print("HINT: Tenant Ready=True Reconciled only means the operator finished a reconcile; readiness here is judged from DaemonSetReady, nodes, keytabs, roster and daemon events")
else:
    parts = c_ready.split("|")[:2]
    print(f"HINT: Tenant Ready condition is {' '.join(parts)}: the operator has not completed a reconcile")

for sev in ("cause", "warn", "unknown", "info"):
    for s, c, ev in FINDINGS:
        if s == sev:
            print(f"FINDING: {s} {c} | {ev}")

if primary:
    print(f"PRIMARY: {primary}")
print(f"CHECKED-OK: {' '.join(ok_list)}")
print("---")
print(SECTIONS)
exit_for(status)

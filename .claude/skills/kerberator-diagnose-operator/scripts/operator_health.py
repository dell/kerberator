#!/usr/bin/env python3
"""operator_health.py [--context C] [--kubeconfig F] [-n NS] [--deployment NAME] [--tail N] [--no-logs]

Read-only health check of the Kerberator OPERATOR CONTROLLER (DG-08): its
Deployment and pods, their events and logs, the installed CRDs and the
admission webhook, and what the versions actually say. Per-Tenant daemons are
out of scope (DG-01); they are only listed so the agent can hand them off.

Output (top lines first, then raw evidence sections):
  STATUS: healthy | compatibility-unverified | running-with-errors | unhealthy
          | installation-incomplete | installation-mismatch | unverified
          | requires-user-selection | error
  FAULT_DOMAIN: none-observed | controller-runtime | controller-image-distribution
          | controller-scheduling | reconciliation-or-configuration
          | operator-installation | operator-installation-crds | version-evidence
          | insufficient-rbac | tenant-daemon-out-of-scope | context-selection
  CONTEXT: <context>
  OPERATOR: <namespace>/<deployment> | not found | unknown
  FINDING: <cause|warn|unknown|info> <code> | <evidence>
  CHECKED-OK: <codes>
  ---
  == section == ...

Never changes anything, never execs, never reads Secrets, never streams logs.
`kubectl krb version` reports the LOCAL plugin only; it is never treated as
operator health or the running operator's version.

Exit: 0 healthy / compatibility-unverified, 4 a problem was found, 3 unverified
(access), 2 requires-user-selection, 1 error. Python 3.8+, standard library only.
"""
import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys

OPERATOR_LABEL = "app.kubernetes.io/name=kerberator"
DAEMON_LABEL = "app.kubernetes.io/name=kerberator-daemon"
# CRD versions the operator release serves (chart 0.10.0: v1alpha1 served + storage).
EXPECTED_CRDS = {
    "tenants.kerberator.dell.com": {"served": {"v1alpha1"}, "storage": "v1alpha1"},
    "principals.kerberator.dell.com": {"served": {"v1alpha1"}, "storage": "v1alpha1"},
}
PULL = ("ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull")
CONFIG = ("CreateContainerConfigError", "CreateContainerError", "RunContainerError")
RECENT_MINUTES = 15
UNREACHABLE = re.compile(r"connection refused|unable to connect|i/o timeout|no such host|deadline exceeded|was refused", re.I)


class Kube:
    def __init__(self, context, kubeconfig):
        self.flags = []
        if kubeconfig:
            self.flags += ["--kubeconfig", kubeconfig]
        if context:
            self.flags += ["--context", context]

    def run(self, *args, timeout=60):
        """kubectl <args> <flags> (flags after args: plugins require it). (ok, out, err)."""
        try:
            p = subprocess.run(["kubectl", *args, *self.flags], capture_output=True, text=True, timeout=timeout)
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


def denied(err):
    return "forbidden" in err.lower() or "unauthorized" in err.lower()


def one_line(s, n=240):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n] + "…"


def parse_time(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def age(t, now):
    if t is None:
        return "?"
    m = int((now - t).total_seconds() // 60)
    return f"{m}m" if m < 120 else f"{m // 60}h"


def cond(obj, ctype):
    for c in (obj.get("status") or {}).get("conditions") or []:
        if c.get("type") == ctype:
            return c
    return {}


def image_tag(image):
    image = image.split("@", 1)[0]
    return image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""


def norm_version(v):
    """'v0.10.0 (unknown, go1.26.8, linux/amd64)' -> '0.10.0': the version, not the build info."""
    return ((v or "").strip().split() or [""])[0].lstrip("v")


# ---------------------------------------------------------------- report

class Report:
    def __init__(self):
        self.findings, self.ok, self.sections = [], [], []

    def add(self, sev, code, evidence):
        f = (sev, code, one_line(evidence, 400))
        if f not in self.findings:
            self.findings.append(f)

    def checked(self, code):
        self.ok.append(code)

    def section(self, title, lines):
        self.sections.append((title, lines))

    def has(self, *codes):
        return any(c in codes for _, c, _ in self.findings)


def parse_log_line(line):
    """controller-runtime production logs are JSON: level, ts, msg, error,
    controller, namespace/name. Development (console) logs are also accepted."""
    line = line.strip()
    if line.startswith("{"):
        try:
            d = json.loads(line)
            return {"level": str(d.get("level", "")).lower(), "ts": d.get("ts"), "msg": d.get("msg", ""),
                    "error": d.get("error", ""), "controller": d.get("controller", ""),
                    "object": "/".join(x for x in (d.get("namespace"), d.get("name")) if x),
                    "version": d.get("version") if d.get("msg") == "kerberator-operator" else None}
        except json.JSONDecodeError:
            pass
    m = re.match(r"^\S+\s+(ERROR|INFO|DEBUG|WARN)\s+(.*)$", line)
    if m:
        return {"level": m.group(1).lower(), "ts": None, "msg": m.group(2), "error": "",
                "controller": "", "object": "",
                "version": (re.search(r'"version":\s*"([^"]+)"', line) or [None, None])[1]
                if "kerberator-operator" in line else None}
    return None


def log_ts(ts):
    if isinstance(ts, (int, float)):
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return parse_time(ts) if isinstance(ts, str) else None


def main():
    ap = argparse.ArgumentParser(usage=__doc__.splitlines()[0])
    ap.add_argument("--context")
    ap.add_argument("--kubeconfig")
    ap.add_argument("-n", "--namespace", help="operator namespace, if known")
    ap.add_argument("--deployment", help="operator Deployment name, if known")
    ap.add_argument("--tail", type=int, default=300, help="log lines per container (default 300)")
    ap.add_argument("--no-logs", action="store_true", help="skip reading operator logs")
    args = ap.parse_args()
    if not shutil.which("kubectl"):
        print("STATUS: error\nFAULT_DOMAIN: none-observed\nCONTEXT: -\nOPERATOR: unknown\n"
              "FINDING: unknown kubectl-missing | kubectl not found on PATH")
        return 1

    now = dt.datetime.now(dt.timezone.utc)
    r = Report()

    # ---- 1. which cluster ------------------------------------------------------
    cfg = ["--kubeconfig", args.kubeconfig] if args.kubeconfig else []
    contexts = subprocess.run(["kubectl", "config", "get-contexts", "-o", "name", *cfg],
                              capture_output=True, text=True).stdout.split()
    current = subprocess.run(["kubectl", "config", "current-context", *cfg],
                             capture_output=True, text=True).stdout.strip()
    if not args.context and len(contexts) > 1:
        # Several clusters and none chosen: don't contact any of them.
        print("STATUS: requires-user-selection\nFAULT_DOMAIN: context-selection\nCONTEXT: -\nOPERATOR: unknown")
        print(f"HINT: contexts={len(contexts)}: {', '.join(contexts)} (current: {current or 'none'}); "
              "ask the user which one, then re-run with --context")
        return 2
    context = args.context or current or (contexts[0] if contexts else "")
    kube = Kube(args.context or None, args.kubeconfig)

    # ---- 2. operator Deployment ---------------------------------------------------
    access_denied = None
    if args.namespace and args.deployment:
        dep, err = kube.json("-n", args.namespace, "get", "deployment", args.deployment)
        deps = [dep] if dep else []
    elif args.namespace:
        lst, err = kube.json("-n", args.namespace, "get", "deployments", "-l", OPERATOR_LABEL)
        deps = (lst or {}).get("items", [])
    else:
        lst, err = kube.json("get", "deployments", "-A", "-l", OPERATOR_LABEL)
        deps = (lst or {}).get("items", [])
    if err and UNREACHABLE.search(err):
        print(f"STATUS: error\nFAULT_DOMAIN: none-observed\nCONTEXT: {context}\nOPERATOR: unknown\n"
              f"FINDING: unknown cluster-unreachable | {one_line(err.splitlines()[-1])}")
        return 1
    if err and denied(err):
        access_denied = err
        where = f"namespace {args.namespace}" if args.namespace else "all namespaces"
        r.add("unknown", "operator-access",
              f"cannot list Deployments in {where} ({one_line(err)}); operator health is unverified. "
              "Needed: get/list deployments, pods, events and pods/log in the operator's namespace")
    elif err and "NotFound" not in err:
        r.add("unknown", "operator-deployment", f"could not read the operator Deployment: {one_line(err)}")

    dep = deps[0] if deps else None
    if len(deps) > 1:
        r.add("warn", "multiple-operators", "more than one Deployment carries " + OPERATOR_LABEL + ": " +
              ", ".join(f"{d['metadata']['namespace']}/{d['metadata']['name']}" for d in deps)
              + "; two controllers reconciling the same CRDs will fight")
    if not dep and not access_denied and (not err or "NotFound" in err):
        r.add("cause", "operator-not-found",
              f"no operator Deployment ({OPERATOR_LABEL}) in "
              f"{'namespace ' + args.namespace if args.namespace else 'any namespace'}: not installed, or installed "
              "under another name (pass -n/--deployment)")

    op_ns = dep["metadata"]["namespace"] if dep else (args.namespace or "")
    op_name = dep["metadata"]["name"] if dep else (args.deployment or "")
    op_image, op_args, op_labels = "", [], {}
    if dep:
        spec, st = dep.get("spec") or {}, dep.get("status") or {}
        ctr = next((c for c in (spec.get("template", {}).get("spec", {}).get("containers") or [])
                    if c.get("name") == "manager"), None) or \
            ((spec.get("template", {}).get("spec", {}).get("containers") or [{}])[0])
        op_image, op_args = ctr.get("image", ""), ctr.get("args") or []
        op_labels = dep["metadata"].get("labels") or {}
        want, ready, avail = spec.get("replicas", 1), st.get("readyReplicas", 0), st.get("availableReplicas", 0)
        av, pr = cond(dep, "Available"), cond(dep, "Progressing")
        lines = [f"deployment:  {op_ns}/{op_name}",
                 f"replicas:    desired={want} ready={ready} available={avail} updated={st.get('updatedReplicas', 0)}",
                 f"image:       {op_image}",
                 f"chart:       {op_labels.get('helm.sh/chart', '?')}  app version label: {op_labels.get('app.kubernetes.io/version', '?')}",
                 f"Available:   {av.get('status', '?')} {av.get('reason', '')}: {one_line(av.get('message'))}",
                 f"Progressing: {pr.get('status', '?')} {pr.get('reason', '')}: {one_line(pr.get('message'))}",
                 "args:        " + " ".join(a for a in op_args if not a.startswith("--metrics") and not a.startswith("--health"))]
        r.section("operator Deployment", lines)
        if want == 0:
            r.add("cause", "operator-scaled-to-zero", f"{op_ns}/{op_name} has replicas=0: no controller is running")
        elif ready < want:
            r.add("cause", "operator-not-ready", f"{op_ns}/{op_name} ready {ready}/{want}, Available={av.get('status', '?')}")
        else:
            r.checked("operator-ready")
        if pr.get("reason") == "ProgressDeadlineExceeded":
            r.add("warn", "rollout-stuck", f"rollout exceeded its progress deadline: {one_line(pr.get('message'))}")

    # ---- 3. operator pods and their events -------------------------------------------
    pod_lines, ev_lines, pods = [], [], []
    pod_problem = None
    if dep:
        sel = ",".join(f"{k}={v}" for k, v in ((dep.get("spec") or {}).get("selector", {}).get("matchLabels") or {}).items())
        plist, perr = kube.json("-n", op_ns, "get", "pods", "-l", sel or OPERATOR_LABEL)
        if plist is None:
            r.add("unknown", "operator-pods", f"cannot list operator pods: {one_line(perr)}")
        pods = (plist or {}).get("items", [])
        for p in pods:
            pn, pst = p["metadata"]["name"], p.get("status") or {}
            cs = next((c for c in pst.get("containerStatuses") or [] if c.get("name") == "manager"),
                      (pst.get("containerStatuses") or [{}])[0])
            waiting = (cs.get("state") or {}).get("waiting") or {}
            term = (cs.get("lastState") or {}).get("terminated") or {}
            sched = cond(p, "PodScheduled")
            pod_lines.append(f"{pn}  phase={pst.get('phase')} ready={cs.get('ready', False)} restarts={cs.get('restartCount', 0)} "
                             f"node={(p.get('spec') or {}).get('nodeName') or '-'} "
                             f"waiting={waiting.get('reason') or '-'} lastExit={term.get('reason', '-')}/{term.get('exitCode', '-')}")
            if sched.get("status") == "False":
                pod_problem = pod_problem or ("operator-unschedulable", "controller-scheduling",
                                              f"{pn} not scheduled: {sched.get('reason', '')} {one_line(sched.get('message'))}")
            elif waiting.get("reason") in PULL:
                pod_problem = pod_problem or ("operator-image-pull", "controller-image-distribution",
                                              f"{pn} {waiting['reason']}: {one_line(waiting.get('message'))} (image {cs.get('image') or op_image})")
            elif waiting.get("reason") == "CrashLoopBackOff":
                pod_problem = pod_problem or ("operator-crash-loop", "controller-runtime",
                                              f"{pn} CrashLoopBackOff, restarts={cs.get('restartCount', 0)}, "
                                              f"last exit {term.get('reason', '?')}/{term.get('exitCode', '?')}")
            elif waiting.get("reason") in CONFIG:
                pod_problem = pod_problem or ("operator-config-error", "controller-runtime",
                                              f"{pn} {waiting['reason']}: {one_line(waiting.get('message'))}")
            elif pst.get("phase") == "Running" and not cs.get("ready"):
                pod_problem = pod_problem or ("operator-probe-failing", "controller-runtime",
                                              f"{pn} running but not ready (readiness probe /readyz)")
            elif cs.get("restartCount", 0) > 0 and cs.get("ready"):
                runtime_restart = term.get("reason") == "Unknown" and term.get("exitCode") == 255
                r.add("warn", "operator-restarted", f"{pn} is ready now but restarted {cs['restartCount']} time(s); "
                      f"last exit {term.get('reason', '?')}/{term.get('exitCode', '?')}"
                      + (" (Unknown/255 usually means the node or container runtime restarted, e.g. Docker, "
                         "not that the operator crashed; check for SandboxChanged events)" if runtime_restart else ""))
        if not pods and plist is not None:
            pod_lines.append("(no operator pods)")
        r.section("operator pods", pod_lines)

        evs, eerr = kube.json("-n", op_ns, "get", "events")
        if evs is None:
            r.add("unknown", "operator-events", f"cannot read events in {op_ns}: {one_line(eerr)}")
        else:
            names = {op_name} | {p["metadata"]["name"] for p in pods}
            mine = [e for e in evs.get("items", []) if any(
                (e.get("involvedObject") or {}).get("name", "").startswith(n) for n in names)]
            mine.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "", reverse=True)
            for e in mine[:12]:
                ev_lines.append(f"[{age(parse_time(e.get('lastTimestamp') or e.get('eventTime')), now)} ago] "
                                f"{e.get('type', '')} {e.get('reason', '')} {(e.get('involvedObject') or {}).get('name', '')} "
                                f"(x{e.get('count') or 1}): {one_line(e.get('message'))}")
            r.section("operator events (newest first)", ev_lines or ["(none; Kubernetes keeps events for about an hour)"])
    if pod_problem:
        r.add("cause", pod_problem[0], pod_problem[2])
        st = (dep or {}).get("status") or {}
        if dep and st.get("readyReplicas", 0) >= (dep.get("spec") or {}).get("replicas", 1) > 0:
            r.add("warn", "previous-pod-still-serving",
                  "the rollout is blocked, but a pod from the previous ReplicaSet is still ready: the operator "
                  "keeps running its old version until the new pod starts")

    # ---- 4. operator logs ---------------------------------------------------------------
    running_version, errors = None, {}
    if dep and pods and not args.no_logs:
        log_lines = []
        for p in pods[:2]:
            pn = p["metadata"]["name"]
            restarted = any(c.get("restartCount", 0) for c in (p.get("status") or {}).get("containerStatuses") or [])
            for prev in ([False, True] if restarted else [False]):
                extra = ["--previous"] if prev else []
                ok, out, lerr = kube.run("-n", op_ns, "logs", pn, "-c", "manager", f"--tail={args.tail}", *extra)
                label = f"{pn}{' (previous)' if prev else ''}"
                if not ok:
                    if denied(lerr):
                        r.add("unknown", "operator-logs", f"cannot read logs of {pn}: {one_line(lerr)}")
                    log_lines.append(f"{label}: unavailable ({one_line(lerr, 120)})")
                    continue
                for raw in out.splitlines():
                    e = parse_log_line(raw)
                    if not e:
                        continue
                    if e.get("version") and not prev:
                        running_version = e["version"]
                    if e["level"] in ("error", "dpanic", "panic", "fatal"):
                        key = (e["controller"] or "-", e["object"] or "-", e["msg"])
                        rec = errors.setdefault(key, {"count": 0, "last": None, "error": "", "previous": prev})
                        rec["count"] += 1
                        rec["error"] = e["error"] or rec["error"]
                        t = log_ts(e["ts"])
                        if t and (rec["last"] is None or t > rec["last"]):
                            rec["last"] = t
                        if not prev:
                            rec["previous"] = False
        for (ctl, obj, msg), rec in sorted(errors.items(), key=lambda kv: -kv[1]["count"])[:12]:
            log_lines.append(f"x{rec['count']} {'(previous container) ' if rec['previous'] else ''}"
                             f"controller={ctl} object={obj} last={age(rec['last'], now)} ago: {one_line(msg, 80)}"
                             f"{': ' + one_line(rec['error'], 200) if rec['error'] else ''}")
        r.section(f"operator logs (last {args.tail} lines; error lines grouped)",
                  log_lines or ["(no error lines)"])
        recent = {k: v for k, v in errors.items() if not v["previous"] and
                  (v["last"] is None or (now - v["last"]).total_seconds() < RECENT_MINUTES * 60)}
        # An error about a Tenant/Principal that has since been deleted is history.
        kinds = {"tenant": "tenants.kerberator.dell.com", "principal": "principals.kerberator.dell.com"}
        for (ctl, obj, msg) in list(recent):
            if ctl in kinds and "/" in obj:
                ons, oname = obj.split("/", 1)
                ok_, _, gerr = kube.run("-n", ons, "get", kinds[ctl], oname, "-o", "name")
                if not ok_ and "NotFound" in gerr:
                    del recent[(ctl, obj, msg)]
                    r.add("info", "errors-for-deleted-object",
                          f"error lines for {ctl} {obj}, which no longer exists: history, not a current failure")
        if recent and not pod_problem and not r.has("operator-not-ready", "operator-scaled-to-zero"):
            (ctl, obj, msg), rec = max(recent.items(), key=lambda kv: kv[1]["count"])
            r.add("cause", "reconcile-errors",
                  f"controller running, but {sum(v['count'] for v in recent.values())} error log line(s) in the last "
                  f"{RECENT_MINUTES}m; most frequent: controller={ctl} object={obj}: {one_line(msg, 80)}: "
                  f"{one_line(rec['error'], 200)}")
        elif not recent and dep:
            r.checked("no-recent-controller-errors")
        if any(re.search(r"x509|certificate|tls", (k[2] + v["error"]).lower()) for k, v in errors.items()):
            r.add("warn", "webhook-tls-errors", "operator logs mention TLS/certificate errors (webhook serving cert)")
    elif dep and args.no_logs:
        r.add("unknown", "operator-logs", "logs not read (--no-logs)")

    # ---- 5. CRDs ---------------------------------------------------------------------------
    crd_lines = []
    for name, want in EXPECTED_CRDS.items():
        crd, cerr = kube.json("get", "crd", name)
        if crd is None:
            if "NotFound" in cerr or "not found" in cerr:
                r.add("cause", "crd-missing", f"CustomResourceDefinition {name} is not installed")
                crd_lines.append(f"{name}: MISSING")
            else:
                r.add("unknown", "crd-access", f"cannot read CRD {name}: {one_line(cerr)}")
                crd_lines.append(f"{name}: unreadable")
            continue
        vers = (crd.get("spec") or {}).get("versions") or []
        served = {v["name"] for v in vers if v.get("served")}
        storage = next((v["name"] for v in vers if v.get("storage")), None)
        est = cond(crd, "Established").get("status")
        crd_lines.append(f"{name}: served={sorted(served)} storage={storage} Established={est} "
                         f"(this release expects served {sorted(want['served'])}, storage {want['storage']})")
        if est != "True":
            r.add("cause", "crd-not-established", f"{name} Established={est}: {one_line(cond(crd, 'Established').get('message'))}")
        elif not want["served"] <= served or storage != want["storage"]:
            r.add("cause", "crd-version-mismatch",
                  f"{name} serves {sorted(served)} with storage {storage}; the operator release expects "
                  f"served {sorted(want['served'])} and storage {want['storage']}")
        else:
            r.checked(f"crd-{name.split('.')[0]}")
    r.section("installed CRDs", crd_lines)

    # ---- 6. admission webhook -----------------------------------------------------------------
    wh_lines = []
    webhook_on = any(a.startswith("--webhook-enabled=true") for a in op_args)
    if dep and webhook_on:
        vwcs, werr = kube.json("get", "validatingwebhookconfigurations")
        if vwcs is None:
            wh_lines.append(f"ValidatingWebhookConfigurations unreadable: {one_line(werr, 120)}")
        else:
            mine = [v for v in vwcs.get("items", []) if any(
                ((w.get("clientConfig") or {}).get("service") or {}).get("namespace") == op_ns
                for w in v.get("webhooks") or [])]
            for v in mine:
                for w in v.get("webhooks") or []:
                    svc = (w.get("clientConfig") or {}).get("service") or {}
                    has_ca = bool((w.get("clientConfig") or {}).get("caBundle"))
                    wh_lines.append(f"{v['metadata']['name']} / {w.get('name', '?')}: service {svc.get('namespace')}/{svc.get('name')} "
                                    f"failurePolicy={w.get('failurePolicy')} caBundle={'set' if has_ca else 'EMPTY'}")
                    if not has_ca:
                        r.add("warn", "webhook-ca-missing", f"{v['metadata']['name']} has no caBundle: Principal "
                              "create/update calls can fail TLS (cert-manager not injecting?)")
                    ep, _ = kube.json("-n", svc.get("namespace", op_ns), "get", "endpoints", svc.get("name", ""))
                    ready = sum(len(s.get("addresses") or []) for s in (ep or {}).get("subsets") or [])
                    wh_lines.append(f"  endpoints ready: {ready if ep else 'unreadable'}")
                    if ep is not None and ready == 0:
                        r.add("warn", "webhook-no-endpoints",
                              f"webhook service {svc.get('namespace')}/{svc.get('name')} has no ready endpoints: "
                              "Principal create/update is rejected or blocked until the operator pod is ready")
            if not mine:
                wh_lines.append("(no ValidatingWebhookConfiguration points at the operator's namespace)")
                r.add("warn", "webhook-config-missing", "operator runs with --webhook-enabled=true but no "
                      "ValidatingWebhookConfiguration points at its namespace")
    elif dep:
        wh_lines.append("webhook disabled (--webhook-enabled not set)")
    if wh_lines:
        r.section("admission webhook", wh_lines)

    # ---- 7. versions ------------------------------------------------------------------------
    try:
        vrun = subprocess.run(["kubectl", "krb", "version"], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        vrun = subprocess.CompletedProcess([], 1, "", "")
    # "kubectl-krb v0.10.0" or "kubectl-krb dev (02fbf45a8444-dirty, go1.27.2, darwin/arm64)"
    m = re.search(r"kubectl-krb\s+(\S+)", vrun.stdout) if vrun.returncode == 0 else None
    cli = m.group(1) if m else ""
    img_ver = image_tag(op_image)
    chart_ver = op_labels.get("app.kubernetes.io/version", "")
    r.section("versions (what each one measures)", [
        f"kubectl-krb plugin (LOCAL machine only, not the cluster): {cli or 'not available'}",
        f"operator image tag (configured in the Deployment):        {img_ver or '-'}",
        f"operator running version (from its startup log line):    {running_version or 'not seen (logs not read, or rotated out of --tail)'}",
        f"Helm chart app version label:                            {chart_ver or '-'}",
    ])
    r.add("info", "cli-version-local-only", f"kubectl krb version = {cli or 'n/a'}: the local plugin build; "
          "it says nothing about the operator's health or running version")
    runtime = running_version or img_ver
    if cli and runtime and norm_version(cli) != norm_version(runtime):
        r.add("warn", "version-difference",
              f"local plugin {cli} vs operator {runtime} ({'running' if running_version else 'image tag'}). A "
              "difference alone doesn't prove anything is broken; compatibility is unverified (release notes)")
    if cli == "dev" or norm_version(runtime) == "dev":
        r.add("info", "dev-build", "a 'dev' version string means a local build, not a release")

    # ---- 8. per-Tenant daemons (out of scope: DG-01) --------------------------------------------
    tlist, terr = kube.json("get", "tenants.kerberator.dell.com", "-A")
    if tlist is not None:
        bad = []
        for t in tlist.get("items", []):
            ds = cond(t, "DaemonSetReady")
            if ds and ds.get("status") != "True":
                bad.append(f"{t['metadata']['namespace']}/{t['metadata']['name']} DaemonSetReady={ds.get('status')} "
                           f"{one_line(ds.get('message'), 80)}")
        r.section("Tenant daemons (DG-01's job; listed for hand-off only)",
                  bad or [f"{len(tlist.get('items', []))} Tenant(s); none report a daemon problem"])
        if bad:
            r.add("info", "tenant-daemons-not-ready",
                  f"{len(bad)} Tenant daemon(s) not ready ({bad[0]}): per-Tenant daemon health is DG-01 "
                  "(kerberator-diagnose-tenant), not the operator controller")

    # ---- STATUS -------------------------------------------------------------------------------
    causes = {c for s, c, _ in r.findings if s == "cause"}
    if access_denied and not dep:
        status, domain = "unverified", "insufficient-rbac"
    elif "operator-not-found" in causes:
        status, domain = "installation-incomplete", "operator-installation"
    elif "crd-missing" in causes or "crd-not-established" in causes:
        status, domain = "installation-incomplete", "operator-installation-crds"
    elif pod_problem:
        status, domain = "unhealthy", pod_problem[1]
    elif causes & {"operator-not-ready", "operator-scaled-to-zero"}:
        status, domain = "unhealthy", "controller-runtime"
    elif "crd-version-mismatch" in causes:
        status, domain = "installation-mismatch", "operator-installation-crds"
    elif "reconcile-errors" in causes:
        status, domain = "running-with-errors", "reconciliation-or-configuration"
    elif r.has("version-difference"):
        status, domain = "compatibility-unverified", "version-evidence"
    elif r.has("tenant-daemons-not-ready"):
        status, domain = "healthy", "tenant-daemon-out-of-scope"
    else:
        status, domain = "healthy", "none-observed"
    if status in ("healthy", "compatibility-unverified") and any(
            s == "unknown" and c in ("operator-logs", "operator-events", "operator-pods") for s, c, _ in r.findings):
        r.add("info", "partial-evidence", "some operator evidence was unreadable; say which parts are unverified")

    print(f"STATUS: {status}")
    print(f"FAULT_DOMAIN: {domain}")
    print(f"CONTEXT: {context}")
    print(f"OPERATOR: {op_ns + '/' + op_name if dep else ('unknown' if access_denied else 'not found')}")
    for sev, code, ev in r.findings:
        print(f"FINDING: {sev} {code} | {ev}")
    print(f"CHECKED-OK: {' '.join(r.ok) or '-'}")
    print("---")
    for title, lines in r.sections:
        print(f"== {title} ==")
        for line in lines:
            print(f"  {line}")
    return {"healthy": 0, "compatibility-unverified": 0, "unverified": 3}.get(status, 4)


if __name__ == "__main__":
    sys.exit(main())

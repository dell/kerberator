---
name: kerberator-diagnose-tenant
description: Check whether a Kerberator Tenant is healthy and, if it is not Ready, narrow down why (DG-01) from status, events, KDC and configuration evidence. Use for any question about a Tenant's health or readiness, whether or not a problem is suspected: "is tenant demo ok", "is tenant demo ready", "health report for tenant demo", "what's wrong with tenant demo", "why is tenant demo not ready", "tenant shows 1/3 nodes", "the daemonset for tenant X is broken", "is the KDC for tenant demo working". Read-only. Do NOT use for listing, finding or describing resources, for Principal/UID-level status (use kerberator-inspect), for why a Principal or UID is failing to authenticate or mint (use kerberator-diagnose-auth-failure), for keytab rotation (use kerberator-rotate-keytab), or to apply fixes.
---

# Diagnose a Tenant that is not Ready (DG-01)

A **Tenant** is one Kerberos realm. The operator turns it into a DaemonSet
`<tenant>-kerberator-daemon`, a roster ConfigMap `<tenant>-roster`, and a
keytab Secret `<tenant>-keytabs`. The daemon on each node mints tickets
for the Tenant's Principals.

**`Ready=True` on a Tenant is not proof of health.** The operator sets
`Ready=True Reconciled` at the end of every reconcile that does not
error, even when no daemon pod is running. Judge readiness from
`DaemonSetReady`, `Nodes ready n/n`, `KeytabsResolved`,
`RosterConsistent`, and the daemons' `Kerberos*` Events.
`references/causes.md` explains each cause code.

## Hard rules

1. **Read-only.** Only run `scripts/diagnose.py` (and `kerberator-inspect`'s `inspect.sh` for discovery). Never run `kubectl apply|patch|edit|delete|rollout|exec|scale`, or any mutating `kubectl krb` subcommand. Suggest fixes as commands for the user to run.
2. **No secret contents.** The script reads keytab Secrets as key names and sizes only. Never read, print, decode or ask for keytab bytes or Secret values.
3. **Evidence, not inference.** Every claimed cause must quote a `FINDING` or a raw evidence line. Do not infer status from `spec` alone or from names.
4. **Unknown is not healthy.** `FINDING: unknown …` means the evidence could not be read (RBAC). Say that area is unverified.
5. **The user chooses the cluster.** Same rule as kerberator-inspect: use the context the user named, or the only one configured. If several exist, ask. Never use a kubeconfig file you found yourself (e.g. one in the repo) unless the user says to.
6. **Logs are allowed, read-only, through the script.** It reads the operator's recent logs when the operator looks implicated, and the last 30 lines of daemon pods that are failing to mint or crash-looping (`--previous` for crash loops). Quote only the relevant lines. Never stream (`-f`) logs. If the user declines log reads, pass `--no-logs`.

## Usage

Run from this skill's base directory:

```
scripts/diagnose.py contexts [--kubeconfig F]
scripts/diagnose.py tenant <name> [-n NS] [--context C] [--kubeconfig F] [--logs | --no-logs] [--tail N]
```

Output:

```
STATUS: healthy | healthy-with-warnings | not-ready | not-found | not-found-in-ns(ns) | not-found-unverified | forbidden | crd-missing | plugin-missing | error
CONTEXT: <context>
HINT: ...
FINDING: <cause|warn|unknown|info> <code> | <evidence>
PRIMARY: <code>              # the most upstream cause; absent when healthy
CHECKED-OK: <codes>          # causes checked and ruled out
---
== section == ...            # raw evidence: describe, spec/status, operator, DaemonSet, pods, events, Principals, Secrets (keys only), ConfigMap, KDC, operator/daemon logs
```

Exit codes: 0 healthy*, 4 not-ready, 2 not-found*, 3 forbidden/crd-missing/plugin-missing, 1 error.
Logs: operator logs are read automatically when the cause looks operator-side (no conditions, operator down, DaemonSet missing, spec not applied); daemon logs when a pod is failing to mint or crash-looping. `--logs` forces the operator read; `--no-logs` skips both.

## Workflow

0. **Choose the cluster.** Run `contexts` unless the user named a context or kubeconfig; follow kerberator-inspect's rules (ask when several exist; name the one used).
1. **Find the Tenant.** If you do not have its exact name and namespace, run `../kerberator-inspect/scripts/inspect.py list -A`. If that is `forbidden` (an account limited to one namespace), run `list` without `-A`, which searches the context's default namespace. If the Tenant is not there, ask the user for its namespace; do not try other namespaces by guessing. Tenant CR name ≠ Kerberos realm (`demo` vs `EXAMPLE.COM`).
2. **Diagnose:** `scripts/diagnose.py tenant <name> -n <ns> [--context C]`. It also checks an in-cluster KDC (Service, ready endpoints, pod start time) named in the Tenant's `krb5.conf`; an external KDC is reported as not checked.
   - A `not-found*` status: explain it as kerberator-inspect does (wrong namespace, unsearchable namespaces, or it does not exist) and stop.
   - `forbidden` / `crd-missing` / `plugin-missing` / `error`: report the status and the raw error. The Tenant's state is unknown.
3. **Narrow down.** Start from `PRIMARY`, the most upstream cause; other causes may be its consequences. For example, a missing DaemonSet also shows up as Principals not Ready. Check the PRIMARY against the raw sections and `references/causes.md`. If the evidence is truncated (`…`), say so.
4. **Use the daemon logs section** for the full mint or crash error, and the KDC section to tell a live KDC outage (`kdc-not-ready`) apart from a failure that has already cleared up (`mint-failure-stale`: the daemon only retries at its next renew tick). If something is still unexplained, say what evidence would settle it.
5. **Report.**

## Limited permissions

Many accounts can read only their own namespace. The Tenant, its DaemonSet, pods, Events, Principals, keytab Secret names and roster ConfigMap live there, so most causes can still be diagnosed. The operator and an in-cluster KDC usually live in other namespaces, so they typically come back as `FINDING: unknown operator-health` and `FINDING: unknown kdc`.

- Give the verdict from the evidence you could read, and say in the first lines that some areas were not checked.
- `CHECKED-OK` lists only checks whose evidence was readable. Never move an `unknown` area into "Ruled out".
- In "Unverified", name each area, the access that would settle it (taken from the FINDING, e.g. "list Deployments in the operator's namespace", "read Services in `kerberator-test-kdc`"), and any indirect evidence, marked as such (e.g. "tickets minted 20m ago, so the KDC answered then").
- If an unreadable area is the likely cause (e.g. mints fail with `Cannot contact any KDC` and the KDC Service is unreadable), say the evidence points there but is unconfirmed, and that someone with access (usually a cluster admin) must check it.

## Report format

Lead with a one-line verdict naming the cluster, for example:

> **`demo-ns/demo` on `kind-dev` is not ready: daemon pods cannot pull their image** (1/2 nodes ready).

Then:

1. **Evidence.** A short table with columns *signal | observed value | source*. Quote `FINDING` lines and raw lines, with ages.
2. **Why this is the cause.** One or two sentences linking the evidence to the effect. Mention the `Ready=True` trap when it applies.
3. **Also found.** Other `cause`/`warn` findings, each marked as either a consequence of PRIMARY or independent.
4. **Ruled out.** A compact list from `CHECKED-OK`, so the user can see what was eliminated (the "narrowing").
5. **Unverified.** Every `unknown` finding, and what access would settle it.
6. **Next step.** The fix from `references/causes.md`, as commands for the user to run, or a handoff: keytab KVNO/rotation goes to kerberator-rotate-keytab; per-Principal node detail goes to kerberator-inspect `describe principal`.

When a few signals point to the same place, keep the report short. When nothing matched (`unexplained`), say no cause was isolated, show the strongest raw evidence, and suggest what to collect next.

Special cases:
- `tenant-disabled`: say the Tenant is **intentionally** disabled (`spec.disabled: true`), not broken.
- `healthy` / `healthy-with-warnings`: say the Tenant **is** ready (`n/n`, all daemons minting) and list the warnings. If the user expected a problem, point them to the Principal level (kerberator-inspect).
- `daemon-node-subset` (info): the Tenant's `spec.daemon.nodeSelector` restricts the daemon to some nodes on purpose. `n/n` counts only the matching nodes, so do not report the other nodes as missing.
- `no-eligible-nodes`: say which shape the finding names. Either no node has the labels, or the matching nodes are tainted/cordoned. For taints, the fix is a `spec.daemon.tolerations` entry, not removing the taint.
- `principal-status-stale` (info): a Principal looks not Ready only because the operator stopped trusting its last mint Event after 5 minutes (a known issue when `renewMinutes` is unset). Say it **is** minting, the status is stale, and give the `renewMinutes` workaround; do not report a fault.
- `principal-no-eligible-nodes`: the Tenant can be healthy while one Principal's own `spec.nodeSelector` matches no node. Name the selector and suggest checking `kubectl krb node-labels`.

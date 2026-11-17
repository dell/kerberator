---
name: kerberator-diagnose-operator
description: Check whether the Kerberator operator controller itself is healthy (DG-08) and tell component-health problems (crash loop, image pull, scheduling, failing probes) apart from installation and configuration problems (missing or mismatched CRDs, admission webhook, reconcile errors, version differences), from its Deployment, pods, events, logs and installed CRDs. Read-only. Use when asked whether the Kerberator operator is running, healthy or up to date, why it keeps restarting or won't start, whether the CRDs are installed or match the release, or what `kubectl krb version` means. Do NOT use for a Tenant's daemon pods or "Tenant not Ready" (use kerberator-diagnose-tenant, DG-01), for a Principal that can't get tickets (use kerberator-diagnose-auth-failure, DG-02), or to install, upgrade or fix anything (kerberator-install-upgrade, OP-11).
---

# Diagnose the Kerberator operator (DG-08)

The **operator** is one controller Deployment (label
`app.kubernetes.io/name=kerberator`, usually in `kerberator-system`). It
reconciles Tenants and Principals, builds each Tenant's daemon DaemonSet, and
serves the Principal admission webhook. This skill is limited to that
controller: its pod, logs, the installed CRDs, the webhook and versions.
**Each Tenant's daemon pods are DG-01's job**; hand them off.

**This skill is read-only.** It never restarts, scales, patches, upgrades or
reinstalls anything.

## Workflow

Run the script from this skill's base directory:
`python3 <skill-dir>/scripts/operator_health.py` (Python 3.8+, standard library only).

**1. Choose the cluster.** If the user named a context, pass `--context`. If
several contexts exist and none was named, the script prints
`STATUS: requires-user-selection` and lists them without contacting any
cluster: ask the user which one. Never pick one yourself, and never use a
kubeconfig file you found in the repository.

**2. Run it** (read-only, one command):
```bash
operator_health.py [--context C] [-n <operator-namespace>] [--deployment <name>] [--no-logs]
```
It finds the operator by label in all namespaces. If the user's access is
limited to one namespace, pass `-n`. It prints `STATUS`, `FAULT_DOMAIN`,
`OPERATOR`, `FINDING` lines and `CHECKED-OK`, then the raw evidence: the
Deployment, pods, events, grouped error log lines, CRDs, webhook, versions, and
Tenant daemons (listed only for hand-off).

**3. Read `STATUS` and `FAULT_DOMAIN`**, then the matching section of
`references/fault-domains.md`:

| STATUS | Means | Typical FAULT_DOMAIN |
|---|---|---|
| `healthy` | Controller ready, CRDs installed and matching, no recent controller errors | `none-observed`, or `tenant-daemon-out-of-scope` when a Tenant's daemon is unhealthy |
| `unhealthy` | The controller process isn't running or ready | `controller-runtime` (crash loop, failing probe, config error), `controller-image-distribution` (image pull), `controller-scheduling` (pod can't be scheduled) |
| `running-with-errors` | Controller is up, but it logged errors in the last 15 minutes while reconciling specific resources | `reconciliation-or-configuration` |
| `installation-incomplete` | Operator or a CRD is missing / not established | `operator-installation`, `operator-installation-crds` |
| `installation-mismatch` | CRDs don't serve the versions this release expects | `operator-installation-crds` |
| `compatibility-unverified` | Everything is up, but the local plugin and operator versions differ | `version-evidence` |
| `unverified` | The user's access can't read the operator | `insufficient-rbac` |
| `requires-user-selection` | Several contexts, none chosen | `context-selection` |

**4. Check the evidence agrees.** Quote the waiting reason, event or log line
behind the verdict. A component problem (`unhealthy`) usually explains the
consequences listed with it: no webhook endpoints, Tenants not reconciling.
Say which finding is the cause and which are consequences.

## Versions: what each one measures

The script lists four versions; keep them apart:
- `kubectl krb version`: the **local plugin** on the user's machine. It says
  nothing about the operator's health or what runs in the cluster.
- The operator **image tag** (configured) and the **running version** from the
  operator's startup log line (what actually started).
- The Helm chart's app-version label.

A difference between the plugin and the operator is **not** proof that
anything is broken; compatibility is unverified until the release notes say
otherwise. Don't invent a supported version-skew rule. A `dev` version means a
local build.

## Report

Lead with one line naming the cluster and operator, e.g.
> **Operator `kerberator-system/kerberator` on `kind-test` is unhealthy: its pod can't pull the image.**

Then:
1. **Evidence:** a short table of *signal | observed value | source*
   (Deployment, pod, event, log line, CRD), with ages.
2. **Why this is the cause**, in one or two sentences, and which other findings
   are its consequences.
3. **Not verified:** each `unknown` finding and what access would settle it
   (e.g. "read pods/log in kerberator-system"). Unknown is never "healthy".
4. **Next step:** one safe action for the user, or a hand-off.

## Hand-offs

| Finding | Hand off to |
|---|---|
| A Tenant's daemon isn't ready (`tenant-daemons-not-ready`) | `kerberator-diagnose-tenant` (DG-01) |
| A Principal can't get tickets | `kerberator-diagnose-auth-failure` (DG-02) |
| Operator or CRDs missing, mismatched, or an upgrade is needed | `kerberator-install-upgrade` (OP-11) |
| Scheduling, node, registry or RBAC changes | The cluster admin; quote the event or error |

## Never

- Change anything: no restart, rollout, scale, patch, delete, apply, upgrade or reinstall.
- Run `kubectl exec`, stream logs (`-f`), or read Secrets (webhook certificates, pull secrets).
- Treat `kubectl krb version` as the operator's version or health.
- Call the operator healthy or broken from evidence you couldn't read.
- Diagnose Tenant daemons here; hand them to DG-01.
- Run `git commit` or `git push`.

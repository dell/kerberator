---
name: kerberator-drain-node
description: Safely guide the documented `kubectl krb drain-node <node>` workflow, inspect the selected cluster and affected Kerberator daemons, execute an explicitly authorized drain or `--uncordon`, and verify the observed node state. Use for OP-07 maintenance of a Kerberator node. Also supports a separate, explicitly authorized ordinary-workload evacuation with `kubectl drain --ignore-daemonsets`.
---

# OP-07 — Drain a node / return a node to service

Use **only** the scripts packaged with this skill and the installed `kubectl`/`kubectl-krb` plugin. No repository source checkout is needed. Python 3 standard library only.

## Contract you must understand before doing anything

The current `kubectl krb drain-node NODE` command **cordons NODE and deletes all pods labeled `app.kubernetes.io/name=kerberator-daemon` on that node**, across namespaces. It **does not evict other workloads**. The `--uncordon` form **only uncordons the node**; it does not recreate pods or validate Kerberos.

**Critical caveat:** Kubernetes DaemonSet pods generally tolerate unschedulable/cordoned nodes. The DaemonSet controller may immediately recreate deleted daemon pods *while the node is still cordoned*. Consequently, **do not promise that the daemon is stopped or that ticket minting is quiesced**. This workflow verifies the observed outcome, not permanent daemon absence. If the user needs workload evacuation, use the **optional `scripts/evacuate.py` workflow below**, only after separately assessing and authorizing that disruption. Never silently substitute it for `kubectl krb drain-node`. If they need guaranteed daemon quiescence, stop and explain that an additional authorized placement change or corrected CLI semantics are required.

## Interaction: discovery first, minimal questions

Discover kubeconfig, available contexts, nodes, ready state, daemon pods, and permissions by running `scripts/preflight.py`. **Do not ask** the user for namespace, daemon Pod name, node labels, Kerberos realm, keytabs, or kubectl commands: these are either discoverable or unnecessary for this operation. No credentials or Secret data are needed.

Ask a question only if (1) several contexts exist and the user did not name one, (2) the target node is unspecified/ambiguous, (3) the user has not authorized the proposed mutation, or (4) the request is actually ambiguous between Kerberator-daemon drain and Kubernetes workload evacuation. Cluster-read failures are **not** a questionnaire: report the RBAC/error cause and required administrator action.

Never silently select a context when multiple exist. Prefer a context or kubeconfig explicitly named by the user. Pin both in every operation. Never use a guessed namespace or the implicit `default` for discovering daemon pods.

## A. Discover and assess (read-only)

First, if a node is not known, list the candidate nodes:

```bash
python3 scripts/preflight.py --mode drain [--context CONTEXT] [--kubeconfig PATH]
```

Then run the full preflight with one exact node:

```bash
python3 scripts/preflight.py --mode drain --node NODE --context CONTEXT --out /tmp/op07-drain-plan.json
```

For return to service, use **a fresh plan**, not the old drain plan:

```bash
python3 scripts/preflight.py --mode uncordon --node NODE --context CONTEXT --out /tmp/op07-uncordon-plan.json
```

Omit `--context` only if preflight confirms exactly one configured context; omit `--kubeconfig` if the ordinary kubeconfig is intended. The scripts display JSON containing the actual selected context, node readiness/cordon status, Kerberator daemon pods, other pods remaining on that node, per-namespace RBAC checks, the proposed effects, and any blockers. The `--out` file is local only and contains no keytab or Secret data.

**Interpret statuses:** `READY` means the verified preconditions permit proposing the command, *not* that the operation has been authorized or that a daemon will stay absent; `CHOICE_REQUIRED` means the target is missing; `BLOCKED` means do not proceed. Report `Forbidden` distinctly from `NotFound` or missing `kubectl`/plugin. Do not bypass `BLOCKED` or install tools, change RBAC, change Pod Security policies, or edit the cluster to make it pass.

If preflight reveals other running pods, explicitly state they will **not** be evacuated. If the request is for maintenance requiring ordinary workload evacuation, follow the optional guarded evacuation workflow below. Do not imply the plugin command alone empties the node.

## B. Show one concise proposal and obtain approval

State:

- Context and exact node; current Ready and cordon state.
- The affected Kerberator daemon pod names, namespaces, and count (the plugin deletes all matching daemons on that node, not just a selected Tenant).
- Other resident workloads that will remain.
- Effects and risks, especially possible immediate DaemonSet recreation, temporary interruption of ticket minting, and existing tickets not being revoked or purged.
- Whether this is **drain** or **return to service**.

Ask once: **“Run `kubectl krb drain-node NODE` on context CONTEXT?”** or **“Return NODE to service with `kubectl krb drain-node NODE --uncordon`?”** An explicit earlier user instruction to perform this exact action on this exact target can count as approval; do not ask twice. Approval for draining does not authorize uncordoning later unless the user already authorized that as part of an unambiguous maintenance workflow.

No execution if the user only asked for an assessment, or if any blocker remains. Do not auto-retry a write.

## C. Execute the single approved operation

After approval, run the bundled executor against the **matching, fresh, READY plan**:

```bash
python3 scripts/execute.py --plan /tmp/op07-drain-plan.json --approve NODE
```

For return to service:

```bash
python3 scripts/execute.py --plan /tmp/op07-uncordon-plan.json --approve NODE
```

The executor rechecks node UID and scheduling state and, for draining, the daemon pod roster before mutation. It then executes exactly one of these installed plugin commands using the pinned context:

```bash
kubectl --context CONTEXT krb drain-node NODE
kubectl --context CONTEXT krb drain-node NODE --uncordon
```

For `PARTIAL_OR_FAILED`, **do not repeat the command blindly**. The plugin updates the node before attempting pod deletion, so it may have made partial changes. Verify and explain observed state, then request new authorization for any recovery action.

## D. Verify real cluster state (read-only)

```bash
python3 scripts/verify.py --plan /tmp/op07-drain-plan.json
# or
python3 scripts/verify.py --plan /tmp/op07-uncordon-plan.json
```

Report actual `spec.unschedulable`, Node Ready, daemon pod identities before/after, replacements appearing after deletion, and any ordinary pods still present. A result of `STATE_VERIFIED_WITH_WARNINGS` is **not** proof of daemon quiescence. A basic successful uncordon is also **not** proof that workloads are healthy or Kerberos minting has recovered.

When the user asks for Kerberos readiness confirmation after uncordon, read relevant Tenant/Principal status and daemon readiness; don't claim tickets are valid from Pod Ready alone. If that evidence is not available, report the verification limit.

**Never automatically uncordon after a drain.** The node may intentionally remain cordoned for maintenance. Only run the return-to-service phase once maintenance is complete and the user wants the node returned.

## E. Optional: evacuate ordinary workloads for maintenance (separate operation)

**Use only if the user specifically wants workloads moved off the node.** This is a different operation from `kubectl krb drain-node`; its primary command is standard `kubectl drain --ignore-daemonsets`, which evicts eligible ordinary workload pods and keeps DaemonSet/mirror pods. Do not invoke both commands by default or assume one substitutes for the other. If users need both, explain the separate effects, plan each, and obtain approval covering each operation. **Prefer normal workload evacuation before deliberately disturbing Kerberator daemons**, with credential coverage on destination nodes already verified.

The additional bundled `scripts/evacuate.py` uses the same discovery-first approach and accepts `plan`, `execute`, and `verify`. It needs no knowledge of Kerberos realms or Secret contents.

1. **Discover (read-only):** `python3 scripts/evacuate.py plan --node NODE --context CONTEXT --out /tmp/op07-evacuate-plan.json`. Without `--node` it lists nodes. It inspects all pods on the node (across namespaces), owner controllers, `emptyDir` volumes, PodDisruptionBudgets, and RBAC. It returns `BLOCKED` instead of enabling dangerous `kubectl drain` flags. It does not itself prove that other nodes have enough CPU, capacity, storage access, or valid Kerberos credential coverage.
2. **Analyze impacts before approval:** Identify all affected workloads, controllers, destination-node eligibility, PDB limits, storage (`hostPath`, local PVs, `emptyDir`), readiness requirements, and whether applications require a Kerberos Principal. Check required Principals and Tenant daemon placement on *possible destination nodes* using status/cluster evidence. If capacity, identity, or data safety cannot be established, stop and report the specific gap instead of pretending the plan is safe. Do not create Principals, modify selectors, or widen credential access automatically. Do not treat `READY` from the script as a production-safety guarantee.
3. **Request explicit disruptive-action approval** for the exact context, node, and ordinary workload eviction; prior permission to run `kubectl krb drain-node` alone does not authorize this. State that workloads can be evicted and potentially suffer downtime. Never automatically enable `--force`, `--delete-emptydir-data`, `--disable-eviction`, `--grace-period=0` or PDB modifications.
4. **Execute once:** `python3 scripts/evacuate.py execute --plan /tmp/op07-evacuate-plan.json --approve NODE`. It rechecks the node UID, cordon state, and pod inventory, then runs `kubectl drain NODE --ignore-daemonsets --timeout=600s` using the pinned context/kubeconfig. On failure, assume partial eviction may have occurred: **do not** blindly retry or uncordon.
5. **Verify:** `python3 scripts/evacuate.py verify --plan /tmp/op07-evacuate-plan.json`. Require node cordoned and zero ordinary running/pending pods left; DaemonSet/static/mirror pods are expected to remain. Additionally inspect the *replacement* workload pods and controllers for readiness, actual destinations, and application-specific Kerberos access if relevant. The script does not claim those secondary checks pass automatically.
6. **Return to service:** After maintenance and separate approval, use **the normal OP-07 `preflight.py --mode uncordon` → `execute.py` → `verify.py`** sequence. This runs `kubectl krb drain-node NODE --uncordon`, not a second drain. Verify node readiness before return. Uncordon makes scheduling possible; it does not automatically move workload replicas back.

If no managed pod can be safely evicted because of a PDB, local storage, scheduling constraints, missing credentials, or insufficient permissions, the right result is **BLOCKED**, not a forced drain. The agent should disclose these blockers and hand off to a cluster administrator or appropriate diagnostic skill as relevant.

## Troubleshooting and hand-offs

| Observation | Agent behavior |
|---|---|
| `kubectl` or `kubectl krb` missing | Report the missing installed dependency; refer to `kubectl-krb` installation documentation, no automatic install |
| Unknown/multiple contexts | Show discovered contexts and ask one targeted selection question |
| Node or cluster read forbidden | Identify missing RBAC; do not assume absence or ask for secrets |
| Cannot `update nodes` / `delete pods` | Stop; note that node updates and pod deletion require elevated permissions |
| Daemon pods reappear on cordoned node | Explain DaemonSet scheduling tolerations; do not repeatedly delete pods or claim quiescence |
| User wants ordinary workloads evacuated | Run `scripts/evacuate.py plan` and follow its separate approval/execution/verification flow; DaemonSet and mirror pods remain |
| Daemon/Principal not ready after return | Hand off to `kerberator-inspect` for CR status; `kerberator-diagnose-auth-failure` for mint/auth failures; `kerberator-configure-node-targeting` for selector/eligible-node problems |
| Node Ready=False or daemon pods are unhealthy | Do not uncordon automatically; provide observed Kubernetes node/pod conditions for admin investigation |
| User requests permanent daemon exclusion or credential revocation | Out of scope; never change DaemonSet/Principal selectors or disable a Principal as an implicit drain side effect |

## Hard rules

- Only one target node per invocation; never mutate the control plane or other nodes without their own explicit review/approval.
- No automatic `kubectl drain` **unless the separate evacuation workflow was requested and authorized**, `delete node`, selector patching, disabling Tenants/Principals, forced eviction, or secret/keytab reads.
- Do not print authentication tokens, kubeconfig contents, keytab bytes, Secret `.data`, or ticket-cache contents.
- Do not commit or push to GitHub.
- Distinguish **node cordoned**, **daemon deletion attempted**, **daemon absent now**, **ordinary pods evacuated**, **Kerberos healthy**, and **node returned to service**. These are different claims.
- Test in a disposable Kubernetes cluster before using the mutation path on a shared/production environment.

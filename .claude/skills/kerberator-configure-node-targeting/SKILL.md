---
name: kerberator-configure-node-targeting
description: Inspect, configure, and verify Kubernetes node targeting for Kerberator Principals using node labels and spec.nodeSelector. Use when a user asks to restrict a Principal to specific nodes, edit its node selector, inspect eligible nodes, or troubleshoot node targeting.
---

# Kerberator Configure Node Targeting

## Purpose

Help operators inspect and configure which nodes can receive a Kerberator Principal's Kerberos credentials. `Principal.spec.nodeSelector` matches Kubernetes Node labels using AND semantics.

## Important behavior

- An absent or empty selector means the Principal can be served on every node where its Tenant daemon runs.
- A selector restricts this to matching nodes. Eligibility always means both: the node matches the Principal selector, and this Tenant actually has a daemon pod running on that node.
- The operator publishes selectors and node labels for daemon evaluation. A daemon that cannot resolve its own node labels fails closed for selector-restricted Principals.
- Placement changes normally reconcile within 30-90 seconds.
- When a node stops matching, the daemon treats that Principal as no longer eligible. With Tenant `spec.daemon.pruneStale: true` (the documented `kubectl krb add-tenant` default), its previously managed ticket cache may be pruned. Workloads relying on that cache may immediately lose Kerberos access.

## Safety rules

1. Begin with read-only inspection and discover labels; never invent or assume them.
2. Never change Node labels without explicit user authorization.
3. Before any selector change, identify the currently eligible Tenant daemon nodes that will stop matching. Explain the possible cache pruning and immediate workload access loss described above, and obtain explicit approval for that ticket-loss impact.
4. Show the exact proposed merge patch and explain its effects. First run it with `--dry-run=server`; only after that succeeds, obtain explicit approval to apply the identical patch without dry-run.
5. Use JSON `null` for a selector key that should be removed from the map. Set the whole `nodeSelector` field to JSON `null` to clear the selector entirely; explain that this makes the Principal eligible on every node where this Tenant daemon runs.
6. Modify only the requested Principal. Never expose Secret data, keytabs, ticket-cache contents, or credentials.
7. Verify an authorized change by inspecting the resulting resource and eligible node status. If permissions or evidence are insufficient, stop and explain what is missing.
8. Never run `git commit` or `git push`.

## Workflow

### Step 1: Identify the Principal

Confirm its name and namespace, then inspect:

    kubectl get principal <principal-name> -n <namespace> -o yaml

Record identity and UID, Tenant reference, `spec.nodeSelector`, Ready condition, per-node status, and `status.tenant.readySummary`. Do not expose Secret data.

### Step 2: Discover labels and Tenant daemon placement

Discover operator-set labels:

    kubectl krb node-labels

Use `kubectl krb node-labels --show-system` to include system labels, or `kubectl krb node-labels --script` for machine-readable output. Inspect actual labels with `kubectl get nodes --show-labels`.

The operator names this Tenant's DaemonSet `<tenant-name>-kerberator-daemon` and labels its pods with `app.kubernetes.io/name=kerberator-daemon`, `app.kubernetes.io/instance=<tenant-name>`, and `app.kubernetes.io/managed-by=kerberator-operator`. Inspect that DaemonSet and verify its controller owner reference points to the requested Tenant:

    kubectl get daemonset <tenant-name>-kerberator-daemon -n <namespace> -o yaml

List candidate pods with the Tenant-specific labels and node placement:

    kubectl get pods -n <namespace> -l 'app.kubernetes.io/name=kerberator-daemon,app.kubernetes.io/instance=<tenant-name>,app.kubernetes.io/managed-by=kerberator-operator' -o wide

Before counting a pod, verify its controller owner reference points to that DaemonSet (and the DaemonSet owner reference points to the requested Tenant). Never assume every daemon pod in the namespace belongs to this Tenant. A namespace-wide `kubectl get daemonset,pods -n <namespace> -o wide` listing can help inspect placement, but is not sufficient to identify this Tenant's pods without checking labels and ownership. If the DaemonSet cannot be inspected because of permissions, preserve the fallback described under Troubleshooting and clearly disclose the limitation.

### Step 3: Determine current targeting

Apply every selector key/value with AND semantics to the actual Node labels. Then intersect those matching nodes with the nodes hosting this Tenant's daemon pods. **Only this intersection is the eligible-node set.** Never use the raw count or complete output of `kubectl get nodes -l ...` as the eligible count.

If the selector is absent or empty, all nodes hosting this Tenant's daemon pods are eligible. Report matching-but-no-daemon nodes separately; they are not eligible. Identify eligible nodes excluded by the proposed selector, if any.

### Step 4: Propose and assess the change

Before editing, report the current and proposed selectors, required labels, the eligible node set before and after the change, and the eligible Tenant daemon nodes that will stop matching. Calculate both sets using selector matches intersected with actual Tenant daemon pod placement, not all nodes returned by a label query.

Explain that caches on newly ineligible nodes may be pruned when `pruneStale` is enabled and workloads may immediately lose Kerberos access. Ask for explicit approval of this ticket-loss impact. If it is not approved, do not proceed.

### Step 5: Configure node targeting

For a newly registered Principal only, the CLI documents repeatable `--node-selector` flags:

    kubectl krb add-principal <tenant-name> <uid> --keytab-file <keytab-file> --node-selector <key>=<value> --node-selector <key>=<value>

For an existing Principal, use a Kubernetes merge patch, scoped to `spec.nodeSelector`. Construct the exact JSON patch based on the user's requested final selector. For example, to set two entries:

    kubectl patch principal <principal-name> -n <namespace> --type=merge -p '{"spec":{"nodeSelector":{"security-zone":"prod-a","tier":"gpu"}}}' --dry-run=server -o yaml

Show the exact patch to the user and explain which selector entries it sets or changes and the resulting eligible nodes and ticket-loss impact. Run this server-side dry-run first. If it succeeds, ask for explicit approval to apply the same patch; do not treat approval for the dry-run or for the impact explanation as approval to write. After approval, apply the exact same patch with `--dry-run=server` removed:

    kubectl patch principal <principal-name> -n <namespace> --type=merge -p '{"spec":{"nodeSelector":{"security-zone":"prod-a","tier":"gpu"}}}'

To remove one key while keeping other selector entries, set that key to JSON `null` in the merge patch, for example `{"spec":{"nodeSelector":{"tier":null}}}`. To clear all restrictions, patch the whole field to JSON `null`, for example `{"spec":{"nodeSelector":null}}`. Explain the resulting eligibility and obtain the required impact approval before proceeding.

Interactive editing is an optional tip for a human operator: `kubectl krb edit principal <principal-name> -n <namespace>` or `kubectl krb tui` (press `s` on a selected Principal). The agent's editing method is the reviewed merge-patch flow above.

### Step 6: Verify the result

Re-read the Principal and its status:

    kubectl get principal <principal-name> -n <namespace> -o yaml
    kubectl krb describe principal <principal-name> -n <namespace>

Recompute eligible nodes as selector-matching nodes intersected with nodes where this Tenant actually has a daemon pod. Do not count all nodes returned by `kubectl get nodes -l`. Compare per-node Principal status against that eligible set.

Inspect `status.tenant.readySummary` and the Ready condition:

- `NoEligibleNodes` means zero eligible nodes.
- `AllDaemonsReady` means all eligible daemon pods are ready.
- `SomeDaemonsNotReady` means some eligible daemon pods are not ready.
- Report readiness as `x/y eligible daemon pods ready` using the operator status.

Allow approximately 30-90 seconds for normal reconciliation, then re-check rather than assuming immediate effect. Do not restart workloads or DaemonSets merely to force reconciliation.

## Troubleshooting

### DaemonSet inspection is permission-restricted

If listing or inspecting the DaemonSet is forbidden, do not treat this as a failed node-targeting check. Use the Tenant desired/ready node counts, Principal per-node status, and available pod placement as supporting evidence. Clearly state that the DaemonSet specification could not be inspected and that the full DaemonSet configuration and eligible set could not be directly verified from that specification. Do not claim the fallback proves it.

### No Nodes Match

When the Principal Ready condition has reason `NoEligibleNodes`, the selector matches zero eligible nodes. Eligible nodes are the intersection of nodes matching the Principal selector and nodes where this Tenant actually runs a daemon pod.

Check label key spelling and values, whether labels exist on intended nodes, and whether the Tenant daemon actually runs on matching nodes. All selector requirements must match. Do not add labels without explicit authorization.

### Principal is not ready or caches were pruned

Prefer:

    kubectl krb describe principal <principal-name> -n <namespace>
    kubectl krb events --uid <uid>
    kubectl krb events --reason KerberosCcachePruned

For mint failures on eligible nodes, refer to `kerberator-diagnose-auth-failure` (DG-02). For requests to enable or disable a Principal, refer to `kerberator-enable-disable-principal` (OP-05). Distinguish selector mismatch, daemon readiness, cache pruning, and mint failures.

### Node labels cannot be resolved

Kerberator fails closed for selector-restricted Principals. Do not remove the selector as an automatic workaround. Explain the placement and possible ticket-loss impact, and investigate the missing label information.

## Testing and reporting

Existing testing was read-only against `uid-2001`. The write path has not been tested on the shared team cluster. The local kind quickstart is the appropriate place to test patching and pruning later. Do not claim those tests passed unless they have actually been run.

Summarize the Principal and namespace, original and final selector, label and daemon-placement evidence, eligible and ineligible nodes, approvals obtained, patch result, readiness as `x/y eligible daemon pods ready`, and unresolved evidence or verification limits. Distinguish observed facts from hypotheses.

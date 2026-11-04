---
name: kerberator-configure-node-targeting
description: Inspect, configure, and verify Kubernetes node targeting for Kerberator Principals using node labels and spec.nodeSelector. Use when a user asks to restrict a Principal to specific nodes, edit its node selector, inspect eligible nodes, or troubleshoot node targeting.
---

# Kerberator Configure Node Targeting

## Purpose

Help operators configure which Kubernetes nodes are eligible to receive a Kerberator Principal's Kerberos credentials.

Node targeting is controlled by Principal.spec.nodeSelector, which matches Kubernetes Node labels.

## Important Behavior

- By default, a Principal is eligible on every node served by its Tenant's DaemonSet.
- A nodeSelector restricts eligibility to nodes matching its requirements.
- Selector requirements use AND semantics: a node must match every specified key and value.
- The operator publishes Principal selectors and Node labels in the roster ConfigMap.
- Daemons periodically evaluate selectors against their own node labels.
- Placement changes usually reconcile within 30-90 seconds.
- If the daemon cannot resolve its own node labels, selector-restricted Principals are skipped (fail closed).
- Node label changes trigger placement re-evaluation.

## Safety Rules

1. Begin with read-only inspection.
2. Never assume a label exists. Discover labels from the cluster.
3. Never change Node labels without explicit user authorization.
4. Never change a Principal selector without explicit user authorization.
5. Before changing a selector, explain which Nodes will become eligible or ineligible.
6. Do not broaden a selector or remove restrictions without explaining the credential-placement impact.
7. Modify only the requested Principal.
8. Never expose keytab contents, ticket-cache contents, or credentials.
9. Verify every authorized change by inspecting the resulting resource and node status.
10. If permissions or evidence are insufficient, stop and explain what is missing.

## Workflow

### Step 1: Identify the Principal

Confirm the Principal name and namespace.

Inspect it:

    kubectl get principal <principal-name> -n <namespace> -o yaml

Record:
- Principal identity and UID.
- Tenant reference.
- Existing spec.nodeSelector, if present.
- Ready condition.
- Per-node status.
- Tenant ready-node summary.

Do not expose Secret data.

### Step 2: Discover Node Labels

Run:

    kubectl krb node-labels

This displays operator-set labels suitable for Principal selectors.

To include system labels:

    kubectl krb node-labels --show-system

For machine-readable output:

    kubectl krb node-labels --script

Inspect actual Node labels:

    kubectl get nodes --show-labels

Never invent labels or assume they exist on every Node.

### Step 3: Determine Current Targeting

If spec.nodeSelector is absent or empty, explain that the Principal follows the default behavior: every node served by its Tenant's DaemonSet.

If a selector exists:
- List all selector requirements.
- Explain AND matching behavior.
- Identify Nodes matching every requirement.
- Identify Nodes excluded by the selector.

For example:

    kubectl get nodes -l 'security-zone=prod-a,tier=gpu'

Use actual selector values from the Principal. Example values are not assumed to exist.

### Step 4: Propose the Change

Before editing, explain:
- Current selector.
- Proposed selector.
- Required labels.
- Nodes that currently match.
- Nodes that would become eligible or ineligible.
- Expected credential-placement impact.

Ask for explicit authorization before changing anything.

If authorization is not provided, stop after the read-only investigation and proposal.

### Step 5: Configure Node Targeting

For a new Principal, the documented CLI supports repeated --node-selector flags:

    kubectl krb add-principal <tenant-name> <uid> --keytab-file <keytab-file> --node-selector <key>=<value> --node-selector <key>=<value>

Use this only when registering a new Principal is requested.

For an existing Principal, the documented editing workflow is:

    kubectl krb edit principal <principal-name> -n <namespace>

Edit only spec.nodeSelector. Preserve all unrelated fields.

The TUI also supports editing a Principal's node selector. Press s on a selected Principal:

    kubectl krb tui

The TUI selector picker uses cluster label discovery.

Do not invent a dedicated node-selector update command. If the documented workflow is unavailable, stop and explain the limitation.

### Step 6: Verify the Result

Re-read the Principal:

    kubectl get principal <principal-name> -n <namespace> -o yaml

Check matching Nodes using all selector requirements:

    kubectl get nodes -l '<key>=<value>'

Inspect Principal status:

    kubectl describe principal <principal-name> -n <namespace>

Verify:
- Stored spec.nodeSelector matches the authorized configuration.
- Expected Nodes match all selector requirements.
- Per-node status reflects expected eligible Nodes.
- Eligible daemon pods report successful mint outcomes.
- Ready condition and ready-node summary are consistent with the intended state.

Allow approximately 30-90 seconds for normal reconciliation. Re-check status instead of assuming an immediate update.

If the result is unexpected, inspect relevant event command syntax:

    kubectl krb events --help

Do not restart workloads or DaemonSets merely to force reconciliation.

## Troubleshooting

###Permission-restricted DaemonSet inspection:

 If listing or inspecting DaemonSets is forbidden, do not treat this as a failed node-targeting check. Use the Tenant's desired/ready node counts, the Principal's per-node status, and available pod placement as supporting evidence for current placement. Clearly state that the DaemonSet specification could not be directly verified. Do not claim that these sources prove the full DaemonSet configuration.

### No Nodes Match

- Check label key spelling and value.
- Check whether labels exist on intended Nodes.
- Remember that all selector requirements must match.
- Do not add labels without explicit authorization.

### Principal Is Not Ready

- Inspect Ready condition and per-node status.
- Check whether intended Nodes match the selector.
- Check daemon readiness and relevant events.
- Allow for normal reconciliation.
- Distinguish selector mismatch from daemon or credential-mint failures.

### Node Labels Cannot Be Resolved

Kerberator fails closed for selector-restricted Principals. Do not remove the selector as an automatic workaround. Explain the credential-placement impact and investigate the missing label information.

## Final Report

Summarize:
1. Principal and namespace inspected.
2. Original and final selector.
3. Labels used as evidence.
4. Matching and non-matching Nodes.
5. Whether a change was made and authorized.
6. Per-node verification results.
7. Unresolved issues or missing evidence.

Clearly distinguish observed facts from hypotheses.

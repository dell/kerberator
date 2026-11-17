---
name: kerberator-diagnose-keytab-kvno-mismatch
description: Diagnose Kerberos authentication failures that may involve keytab KVNO drift or enctype incompatibility. Use when a user reports Preauthentication failed, KerberosMintFailed, or a suspected mismatch between the KVNO observed in a Principal's keytab Secret and the KDC.
---

# Diagnose Keytab or KVNO Mismatches

## Goal

Diagnose whether a Kerberos authentication failure is consistent with a keytab KVNO mismatch, an enctype incompatibility, or another failure. Gather evidence and recommend a next diagnostic or remediation step.

This skill is read-only. Do not rotate keytabs, modify Secrets, restart workloads, or change cluster resources.

## Safety rules

- Never display, print, decode, copy, upload, or summarize keytab contents or key bytes.
- Never use `klist -K` or `ktutil list --keys`.
- Never retrieve Secret data as YAML or JSON, describe Secrets, or decode Secret values.
- Do not modify a Principal, Tenant, Secret, DaemonSet, or pod.
- Do not run `kubectl krb rotate-keytab` as part of diagnosis.
- Do not assume every `Preauthentication failed` error proves KVNO drift.
- Never claim a mismatch is confirmed unless the available evidence supports that conclusion.
- Do not invent command output or assume a KDC comparison succeeded.

## Diagnostic workflow

### 1. Identify the affected resource

Ask for the affected Principal, namespace, and cluster context if they are not known.

Use the user's selected context. If multiple contexts are available and the intended one is unclear, ask before proceeding.

Inspect the Principal:

`kubectl krb describe principal <principal> -n <namespace>`

Review the reported status, per-node results, recent events, and any authentication failure messages. Keep the Principal name, Tenant CR name, and realm distinct.

### 2. Review recent mint failures

Use the documented read-only event commands:

`kubectl krb events --reason KerberosMintFailed`

When the affected UID and namespace are known, narrow the investigation
to that UID and namespace where supported by the CLI. Do not infer that
events from another Principal belong to the affected resource.

If the Principal's UID is known, narrow the events:

`kubectl krb events --uid <uid>`

Record the affected Principal and node, the reason and message, and whether the failure is recent or recurring.

`Preauthentication failed` is consistent with a stale keytab after a KDC key rotation, but it is not sufficient on its own to prove KVNO drift.

If the message instead indicates that the KDC cannot be contacted, investigate connectivity or KDC availability rather than labeling it a keytab mismatch.

### 3. Inspect the KVNO observed by Kerberator

Use the documented KVNO command:

`kubectl krb -n <namespace> kvno <principal>`

If needed, inspect all Principals in the namespace:

`kubectl krb -n <namespace> kvno`

Record the reported KVNO, observation age, and enctypes. Kerberator exposes the observed keytab metadata through fields such as `status.kvnoFromSecret`, `status.enctypesFromSecret`, and `status.keytabObservedAt`.

The CLI may suggest `rotate-keytab` when recent mint failures exist. Treat
that as a suggestion, not proof of KVNO drift, and do not execute it as
part of this diagnostic skill.

An observation age alone does not establish that the keytab is stale. Note missing or unobserved metadata rather than guessing its value.

### 4. Compare with the KDC

When an appropriately configured environment with access to the relevant KDC is available, compare the keytab's observed KVNO with the KDC-side result using the documented comparison:

`kvno <principal>`

Run this command against the intended realm and KDC using the site's normal authentication and configuration. Do not assume the environment is configured correctly without verifying it.

Interpret the results carefully:

- **Evidence consistent with KVNO drift:** Kerberator reports KVNO N for the Secret, the KDC-side check indicates the principal is using KVNO N+1, and the affected Principal has matching authentication failures.
- **KVNO mismatch not established:** The comparison is unavailable, ambiguous, or does not show a discrepancy. Report what remains unverified.
- **Possible enctype incompatibility:** The keytab's reported enctypes may not include an enctype the KDC can issue. Recommend checking compatibility with the KDC administrator. Do not claim an enctype mismatch without supporting evidence.
- **Other or unclear failure:** If the evidence does not support either diagnosis, report the observed error and recommend the next relevant check instead of forcing a KVNO explanation.

Do not expose key material to perform any comparison.

### 5. Report the diagnosis

Structure the response as follows:

1. **Observed failure:** State the actual event reason and message.
2. **Evidence:** State the Principal, namespace, observed KVNO, observation age, enctypes, and KDC comparison result when available.
3. **Assessment:** Classify the result as confirmed by available evidence, consistent with a suspected mismatch, not established, or more consistent with another cause.
4. **Remaining uncertainty:** Identify missing data or checks that could change the assessment.
5. **Recommended next step:** Give a specific, safe action based on the evidence.

Never invent values, omit contradictory evidence, or report an unverified hypothesis as a confirmed diagnosis.

## Remediation boundary

If the evidence supports KVNO drift, explain that the repository documents the `rotate-keytab` workflow for updating the keytab Secret and allowing the daemons to re-mint.

Recommend that the authorized operator follow the documented rotation procedure. Do not perform the rotation in this skill.

Do not recommend simply applying a new Secret as a complete fix: the repository documents that updating a Secret alone may not immediately trigger a new mint.

# Kerberator Event Interpretation Reference

This is the shared reference for interpreting Kerberator events. The examples below are known event reasons referenced in the repository; this is not an exhaustive event catalog. Always use the full Event object and the implementation/documentation for the installed version.

## General interpretation rules

- An event records a reported occurrence. It does not, by itself, prove that the condition is still present.
- A reason categorizes an event; the full message provides additional detail. Do not replace the message with an assumed explanation or rely on a CLI-truncated table.
- A historical event can remain visible after the underlying condition changes. Check available timestamps, age, count, and series.
- Repeated events indicate repeated reporting, not necessarily independent root causes.
- A numeric UID alone does not establish Principal ownership. Correlate the Event namespace, the Principal's Tenant reference, and the involved daemon Pod's Tenant labels. If that correlation is not unique, report ambiguity and do not combine evidence across Tenants.
- `KerberosPrincipalDisabled` and `KerberosPrincipalEnabled` are operator events recorded on the Principal object. They are not daemon-Pod events.
- An event's absence does not prove that an operation succeeded. Events may be filtered, expired, inaccessible, or not emitted for that operation.
- A finite `--request-timeout` is required on every event retrieval. A timeout means the evidence is unavailable; it does not mean that no events exist.
- `Warning`/`Normal` type, when shown, is event classification; it is not a complete health assessment.
- Correlate with current Principal/Tenant status and relevant daemon evidence before drawing a diagnosis.

## Known event reasons

### `KerberosMintSucceeded`

**Reports:** The daemon reported a successful mint on the involved node at the event time.

**Establishes:** A successful mint on that node at that time.

**Does not establish:** That the ticket is still valid, that other nodes succeeded, or that the current keytab works. A success before the Principal's `status.keytabObservedAt` describes the previous observed keytab, not the current keytab.

**Correlate with:** Full Event timestamps and count/series, the uniquely correlated Principal/Tenant/daemon Pod, per-node Principal status, and `status.keytabObservedAt`.

### `KerberosMintFailed`

**Reports:** A daemon reported a failure while minting Kerberos credentials for a Principal. The full event message may contain the underlying error.

**Establishes:** The reported mint attempt failed at the time represented by the event.

**Does not establish:** The root cause by itself; that the keytab KVNO is wrong; that the KDC is unreachable; that every node failed; or that the Principal is still failing now.

**Correlate with:** Full message, timestamps and count/series, event UID and node, namespace/Tenant/daemon-Pod ownership, Principal per-node status, current mint state, and relevant daemon logs. For `Preauthentication failed`, do not conclude KVNO drift without a KDC-side comparison.

### `KerberosKeytabMissing`

**Reports:** The event message reports a missing keytab condition.

**Establishes:** The component emitted an event reporting that condition at the event time.

**Does not establish:** That the keytab is still missing, that all nodes are affected, or why it was missing.

**Correlate with:** The uniquely correlated Principal, UID, namespace, Tenant, daemon Pod, current status, and safe keytab metadata. Never print or decode keytab contents or Secret data.

### `KerberosPrincipalDisabled` and `KerberosPrincipalEnabled`

**Reports:** An operator-recorded Principal disabled/enabled transition. These events are attached to the Principal object, not emitted on daemon Pods.

**Establishes:** The corresponding transition was reported.

**Does not establish:** The Principal's current disabled state if later changes may have occurred, or whether authentication succeeds.

**Correlate with:** The involved Principal's namespace, name, and UID; current Principal spec and status; and event timestamps. Do not use an old transition event as a substitute for current resource inspection.

### `KerberosCcachePruned`

**Reports:** A daemon event associated with Kerberos credential-cache pruning.

**Establishes:** The event reports a cache-pruning occurrence.

**Does not establish:** Which credentials were removed, the full reason for pruning, whether a user-facing authentication failure occurred, or whether the cache is currently empty.

**Correlate with:** Full message, timestamps and count/series, uniquely correlated UID/namespace/Tenant/daemon Pod when present, current Principal status, and the node-targeting or cache-pruning workflow that produced the event.

## Common overclaims to avoid

| Event evidence | Do not conclude |
|---|---|
| `KerberosMintSucceeded` | “The ticket is still valid,” “every node succeeded,” or “the current keytab works.” |
| `KerberosMintSucceeded` before `status.keytabObservedAt` | “The current keytab succeeded.” |
| `KerberosMintFailed` | “The KVNO is definitely wrong.” |
| `Preauthentication failed` in a message | “The KDC definitely rotated its key.” |
| KDC-unreachable text | “The keytab is stale.” |
| `KerberosKeytabMissing` | “The Secret is still missing now.” |
| `KerberosPrincipalDisabled` | “The Principal is currently disabled.” |
| `KerberosPrincipalEnabled` | “The Principal is healthy and can authenticate.” |
| `KerberosCcachePruned` | “All tickets were invalidated” or “authentication must fail.” |
| Same UID without namespace/Tenant/daemon-Pod correlation | “These events belong to this Principal.” |
| No matching events returned | “The operation succeeded” or “the event never occurred.” |

## Escalation and uncertainty

If the reason is unknown, preserve the exact reason and full message, and state that its semantics are not established by this reference. Retrieve it with a namespace-scoped, read-only `kubectl get events` command that has a finite `--request-timeout`; check the event-producing code or version-matched documentation before interpreting it.

If event access is forbidden, times out, or the API server is unavailable, report that event evidence could not be retrieved. Do not describe the result as an empty event list.

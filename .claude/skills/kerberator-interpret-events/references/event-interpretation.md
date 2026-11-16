# Kerberator Event Interpretation Reference

This is the shared reference for interpreting Kerberator events. The examples below are known event reasons referenced in the repository; this is not an exhaustive event catalog. Always use the actual event message and the implementation/documentation for the installed version.

## General interpretation rules

- An event records a reported occurrence. It does not, by itself, prove that the condition is still present.
- A reason categorizes an event; the message provides additional detail. Do not replace the message with an assumed explanation.
- A historical event can remain visible after the underlying condition changes. Check timestamps, age, and count.
- Repeated events indicate repeated reporting, not necessarily independent root causes.
- An event associated with one UID must not be attributed to another UID.
- An event's absence does not prove that an operation succeeded. Events may be filtered, expired, inaccessible, or not emitted for that operation.
- `Warning`/`Normal` type, when shown, is event classification; it is not a complete health assessment.
- Correlate with current Principal/Tenant status and relevant daemon evidence before drawing a diagnosis.

## Known event reasons

### `KerberosMintFailed`

**Reports:** A daemon reported a failure while minting Kerberos credentials for a Principal. The event message may contain the underlying error.

**Establishes:** The reported mint attempt failed at the time represented by the event.

**Does not establish:** The root cause by itself; that the keytab KVNO is wrong; that the KDC is unreachable; that every node failed; or that the Principal is still failing now.

**Correlate with:** The event UID and node, Principal per-node status, current mint state, the exact error message, and relevant daemon logs. For `Preauthentication failed`, do not conclude KVNO drift without a KDC-side comparison.

### `KerberosKeytabMissing`

**Reports:** The event message reports a missing keytab condition.

**Establishes:** The component emitted an event reporting that condition at the event time.

**Does not establish:** That the keytab is still missing, that all nodes are affected, or why it was missing.

**Correlate with:** The affected Principal, UID, namespace, current status, and safe keytab metadata. Never print or decode keytab contents or Secret data.

### `KerberosPrincipalDisabled` and `KerberosPrincipalEnabled`

**Reports:** A Principal disabled/enabled transition. The repository documents these events on transition.

**Establishes:** The corresponding transition was reported.

**Does not establish:** The Principal's current disabled state if later changes may have occurred, or whether authentication succeeds.

**Correlate with:** The current Principal spec and status, plus event timestamps. Do not use an old transition event as a substitute for current resource inspection.

### `KerberosCcachePruned`

**Reports:** An event associated with Kerberos credential-cache pruning.

**Establishes:** The event reports a cache-pruning occurrence.

**Does not establish:** Which credentials were removed, the full reason for pruning, whether a user-facing authentication failure occurred, or whether the cache is currently empty.

**Correlate with:** The event message, affected UID/node when present, current Principal status, and the node-targeting or cache-pruning workflow that produced the event.

## Common overclaims to avoid

| Event evidence | Do not conclude |
|---|---|
| `KerberosMintFailed` | "The KVNO is definitely wrong." |
| `Preauthentication failed` in a message | "The KDC definitely rotated its key." |
| KDC-unreachable text | "The keytab is stale." |
| `KerberosKeytabMissing` | "The Secret is still missing now." |
| `KerberosPrincipalDisabled` | "The Principal is currently disabled." |
| `KerberosPrincipalEnabled` | "The Principal is healthy and can authenticate." |
| `KerberosCcachePruned` | "All tickets were invalidated" or "authentication must fail." |
| No matching events returned | "The operation succeeded" or "the event never occurred." |

## Escalation and uncertainty

If the reason is unknown, preserve the exact reason and message and state that its semantics are not established by this reference. Check the event-producing code or version-matched documentation before interpreting it.

If event access is forbidden or the API server is unavailable, report that event evidence could not be retrieved. Do not describe the result as an empty event list.

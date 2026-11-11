# Interpret `describe tenant`

Read this reference only when an observed Tenant field, condition, or unexpected CLI output needs interpretation. Use the same meanings for native API status reads. Record the evidence and use the parent skill's hand-offs for diagnosis; no source checkout is required.

| Observed field / condition | Meaning and required interpretation |
|---|---|
| `Nodes ready` | Tenant `status.readySummary`, built from DaemonSet `numberReady/desiredNumberScheduled`. Require equal counts with **desired > 0**, plus placement matching the user's intent. `0/0` is not a ready installation. |
| `Ready=True Reconciled` | Reconciliation reached its end. This is set independently of the other conditions and is not an overall health guarantee. |
| `DaemonSetReady=True AllPodsReady` | DaemonSet observed, desired > 0, ready equals desired. `False SomePodsNotReady` includes partial readiness and 0/0; `Unknown DaemonSetNotObserved` means not observed. |
| `RosterConsistent=True AllUIDsUnique` | No duplicate eligible UIDs in this Tenant's aggregation. `False DuplicateUID` rejects colliding entries. This does not establish cross-Tenant host-cache isolation. |
| `KeytabsResolved=True AllKeytabsResolved` | Eligible source references resolved. `False SomeKeytabsMissing` means at least one could not resolve. True is not proof of valid keytab content or a successful KDC exchange. |
| `Principals` | `status.principalCount`, normally all referring Principal CRs including disabled/deleting ones, not the active roster size. |
| `Roster hash` | Digest of aggregated roster content, not proof that every daemon has consumed it or that krb5.conf reached every running pod. |
| Owned children | Names are printed by convention, not looked up for existence or ownership. Verify the actual objects and controller ownerReference UID separately. |
| Principals table | UID, Kerberos principal, keytab Secret/key, observed KVNO, READY, ready/desired nodes. A missing table does not necessarily prove no Principals: the CLI ignores Principal-list errors. Verify with a separate explicit Principal list and report failures. |

Missing or Unknown conditions leave readiness unverified. An empty Tenant can have ready infrastructure without testing the KDC; cache-file readiness is not proof of valid or fresh tickets. Keep those limits explicit in the creation report.

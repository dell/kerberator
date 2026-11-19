---
name: kerberator-interpret-events
description: Interpret Kerberator Kubernetes Events, retrieve full event details, correlate daemon events safely to a Principal, and explain what the evidence establishes. Use when asked what Kerberator events mean or when a diagnostic workflow needs event evidence.
---

# Interpret Kerberator Events

## Goal

Summarize relevant Kerberator events and explain their evidentiary limits. This skill is independently usable for questions such as “What do these events mean?” It is also the shared event interpretation reference for other diagnostic skills.

This skill is read-only. It does not remediate event causes or modify cluster resources.

## Safety rules

- Use the user's selected Kubernetes context. Never silently switch clusters.
- Keep queries in the identified namespace unless the user explicitly requests a wider scope.
- Do not create, edit, delete, patch, or restart resources.
- A numeric UID alone does not establish Principal ownership. Correlate namespace, the Principal's `spec.tenantRef.name`, and the daemon Pod's Tenant ownership. If that correlation is not unique, report ambiguity and keep each Tenant's evidence separate.
- `KerberosPrincipalDisabled` and `KerberosPrincipalEnabled` are operator events recorded on the Principal object, not daemon-Pod events.
- Do not treat an event as proof that the reported condition is still present.
- Do not infer a root cause from an event reason alone. Use the full event message.
- Do not invent event fields, timestamps, reasons, or command output.
- Events may describe earlier attempts. Check timestamps, count, and series before calling them current or recurring.
- Give every Kubernetes request a finite `--request-timeout`, such as `15s`. If it times out, report that as unavailable evidence; it does not mean that no events exist.

## Workflow

### 1. Establish scope

Use the selected context and the identified namespace. If either is unclear, ask which context or namespace to inspect. Do not switch context. Do not use `-A` unless the user requests a cross-namespace review.

If a Principal is identified, record its namespace, name, UID, and Tenant reference from read-only Principal/Tenant inspection. A shared UID across Tenants in one namespace is possible, so UID is only one correlation value.

### 2. Retrieve daemon events

The curated command displays only the four recognized daemon reasons, truncates messages at 100 characters, and omits Event counts and full timestamps. Use it as a locator, not as the complete evidence:

```bash
kubectl krb --request-timeout=15s events -n <namespace>
kubectl krb --request-timeout=15s events --uid <uid> -n <namespace>
kubectl krb --request-timeout=15s events --reason <reason> -n <namespace>
```

Do not add `-A` unless the requested scope is all namespaces. Preserve the active context; if necessary, pass the user's already selected context explicitly with `--context <context>` to each command.

For full evidence, retrieve the Event objects in that same namespace. Use the event list to find objects matching the displayed reason, daemon Pod, and UID annotation; then retrieve an individual object when needed:

```bash
kubectl --request-timeout=15s get events -n <namespace> -o yaml
kubectl --request-timeout=15s get event <event-name> -n <namespace> -o yaml
```

Read the full `message`, `count`, `series`, and available timestamps (`eventTime`, `firstTimestamp`, `lastTimestamp`, and `metadata.creationTimestamp`). Treat a displayed message that reaches the CLI's 100-character limit, or any missing metadata, as potentially incomplete. Do not interpret the table's truncated message; base interpretation on the full Event object. If full details cannot be retrieved or the request times out, say the evidence is unavailable.

### 3. Retrieve controller and unknown reasons

The curated command omits controller events, including `KerberosPrincipalDisabled` and `KerberosPrincipalEnabled`, and any reason it does not recognize. These operator events are recorded on the Principal object. For controller events, use a namespace-scoped query on the Principal involved object. For an unknown reason, inspect the namespace-scoped Event list so events on daemon Pods and other involved objects are included:

```bash
kubectl --request-timeout=15s get events -n <namespace> --field-selector involvedObject.kind=Principal,involvedObject.name=<principal> -o yaml
kubectl --request-timeout=15s get events -n <namespace> -o yaml
```

Review the full objects and match the Principal namespace, name, and UID where available. Do not filter only by reason if the reason is unknown. Keep the selected context unchanged. A timeout means event evidence is unavailable; it is not evidence that no matching events exist.

### 4. Establish daemon-event ownership

Daemon events refer to a Pod (`involvedObject.kind: Pod`) and carry a UID annotation. Before attributing one to a Principal, establish all of the following in the same namespace:

1. The Event's `metadata.annotations["kerberator.dell.com/uid"]` matches the Principal's `spec.uid`.
2. The Principal's `spec.tenantRef.name` identifies the Tenant.
3. The event's involved daemon Pod belongs to that Tenant. Verify the Pod's `app.kubernetes.io/name=kerberator-daemon` and `app.kubernetes.io/instance=<tenant>` labels, and match the Pod name in the Event's involved object.

For example, inspect daemon Pods for the identified Tenant with a read-only query:

```bash
kubectl --request-timeout=15s get pods -n <namespace> -l app.kubernetes.io/name=kerberator-daemon,app.kubernetes.io/instance=<tenant> -o wide
```

If any correlation is missing, conflicting, or matches more than one Tenant/Principal, report the ownership as ambiguous. Do not combine evidence from different Tenants into one Principal diagnosis. Never infer ownership from UID alone.

### 5. Organize and interpret evidence

Consult `references/event-interpretation.md`.

For each relevant event, capture only fields present in the full object:

- Namespace, involved object kind/name, and UID where available
- Tenant and Principal ownership correlation, or the ambiguity
- Reason, full message, and type
- Available event time, first/last timestamps, creation timestamp, and count/series

Group events by uniquely correlated Principal and Tenant, and order them chronologically when timestamps permit. Distinguish a single event from repeated occurrences, recent events from older events, and missing events from unavailable event access. Do not assume events are cleared between diagnostic runs.

Explain what the reason and full message report, what the observation establishes, what it does not establish, and which read-only evidence would reduce uncertainty. An event is evidence of a reported occurrence, not automatically proof of current state, successful recovery, or root cause.

### 6. Correlate with current state when relevant

When asked why a resource is unhealthy or whether an event's condition is still present, correlate events with current read-only resource status. Use the relevant inspection workflow or `kubectl krb describe` for the affected resource. Keep event evidence and current status separate. If they disagree, report both with timestamps.

### 7. Report

Use this structure:

1. **Summary:** Main event pattern and whether it is recent or recurring, if known.
2. **Relevant events:** Resource, namespace, Tenant, reason, full message, timestamps, and count/series when available.
3. **Ownership:** The namespace/Tenant/daemon-Pod correlation, or an explicit ambiguity.
4. **What they establish:** The narrow factual conclusion supported by each event.
5. **What they do not establish:** Current state, root cause, successful recovery, or other unsupported conclusions.
6. **Next checks:** Specific read-only checks that could resolve remaining uncertainty.

If no relevant events are returned, say no matching events were returned for the queried scope and context. A timeout or access failure means evidence was unavailable, not that no event exists.

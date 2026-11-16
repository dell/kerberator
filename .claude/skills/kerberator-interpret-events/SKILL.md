---
name: kerberator-interpret-events
description: Interpret Kerberator Kubernetes Events and explain what they establish, suggest, and do not prove. Use when asked what Kerberator events mean, or when a diagnostic workflow needs event evidence.
---

# Interpret Kerberator Events

## Goal

Summarize relevant Kerberator events and explain their evidentiary limits. This skill is independently usable for questions such as "What do these events mean?" It is also the shared event-interpretation reference for other diagnostic skills.

This skill is read-only. It does not remediate event causes or modify cluster resources.

## Safety rules

- Use the user's selected Kubernetes context. Never silently switch clusters.
- Do not create, edit, delete, patch, or restart resources.
- Do not assume an event belongs to the affected Principal unless its UID or involved object supports that association.
- Do not treat an event as proof that the reported condition is still present.
- Do not infer a root cause from an event reason alone. Read and preserve the actual message.
- Do not invent event fields, timestamps, reasons, or command output.
- Events may describe earlier attempts. Check their timestamps and count before calling them current or recurring.

## Workflow

### 1. Establish scope

If the user has identified a Principal, UID, namespace, or event reason, use those details to narrow the review.

If the intended cluster is unclear because multiple contexts are configured, ask which context to inspect. Do not choose one silently.

### 2. Retrieve events

Use the documented Kerberator events command:

```bash
kubectl krb events
```

To review across namespaces:

```bash
kubectl krb events -A
```

To filter by UID or reason:

```bash
kubectl krb events --uid <uid> -A
kubectl krb events --reason <reason> -A
```

Combine filters only when supported by the installed CLI version. Do not invent flags. If the command fails, report the actual error and do not claim that no events exist.

### 3. Associate and organize the evidence

For each relevant event, capture only fields actually present in the output, such as:

- Namespace and involved object
- UID, when present
- Reason and message
- Event type, when present
- First/last timestamp or age, when present
- Count, when present

Group events by affected resource and reason. Order them chronologically when timestamps permit.

Distinguish:
- A single event from repeated occurrences.
- Recent events from older events.
- Events for the affected UID from events for other UIDs.
- A missing event from an inability to read events.

Do not assume events are cleared between diagnostic runs. An event that remains visible may predate the current incident.

### 4. Interpret without overclaiming

Consult `references/event-interpretation.md`.

For each event, explain:
1. What the reason and message report.
2. What that observation establishes.
3. What it does not establish.
4. What read-only evidence would help confirm the current state or narrow the cause.

An event is evidence of a reported occurrence, not automatically proof of current resource state, successful recovery, or root cause.

### 5. Correlate with current state when relevant

When the user asks why a resource is unhealthy, or whether an event's condition is still present, correlate events with current read-only resource status. Use the relevant inspection workflow or `kubectl krb describe` for the affected resource.

Keep event evidence and current status separate. If they disagree, report the disagreement and timestamps; do not discard either observation.

### 6. Report

Use this structure:

1. **Summary:** The main event pattern and whether it is recent or recurring, if known.
2. **Relevant events:** Resource, namespace, reason, message, timestamp/age, and count when available.
3. **What they establish:** The narrow factual conclusion supported by the event.
4. **What they do not establish:** Current state, root cause, successful recovery, or other conclusions not supported by the event.
5. **Next checks:** Specific read-only checks that would resolve the remaining uncertainty.

If no relevant events are returned, say that no matching events were returned for the queried scope and context. Do not claim that no event ever occurred.

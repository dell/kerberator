// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// Package events consumes the per-UID Kubernetes Events the
// kerberator-daemon emits and projects them into an in-memory
// (pod, uid) cache keyed on the daemon Pod's name + the principal
// UID extracted from each event (annotation preferred, message-grep
// as fallback).
//
// The cache is the sole data source for the per-principal per-node
// branch of Principal.status.nodes[]. When the cache misses (bootstrap
// window, old daemon image without Event emission, or events
// disabled cluster-wide) the principal reconciler falls back to
// pod-readiness rollup.
//
// Two consumers write to the cache:
//
//   - EventReconciler (this package) watches corev1.Event and
//     ingests any event whose involvedObject.kind=Pod and whose
//     reason is one of the four Kerberos* reasons defined in
//     the shared/events package.
//
//   - The principal finalizer (internal/controller) calls
//     Cache.EvictUID when a principal CR is being deleted, so a
//     stale event from a previous UID incarnation never
//     resurrects a deleted principal.
//
// One consumer reads: BuildNodeStatuses (internal/controller)
// takes a snapshot per (pod, uid) lookup and mixes it with
// pod-readiness data to produce NodeStatus values.
package events

import (
	"regexp"
	"strconv"

	corev1 "k8s.io/api/core/v1"

	sharedevents "github.com/dell/kerberator/shared/events"
)

// Re-exports from shared/events so callers within the operator
// module don't need a second import statement just to reference
// reason strings. Keeps the "one import for events consumption"
// property for readability.
const (
	ReasonMintSucceeded = sharedevents.ReasonMintSucceeded
	ReasonMintFailed    = sharedevents.ReasonMintFailed
	ReasonCcachePruned  = sharedevents.ReasonCcachePruned
	ReasonKeytabMissing = sharedevents.ReasonKeytabMissing

	// UIDAnnotationKey is re-exported for the same reason.
	UIDAnnotationKey = sharedevents.UIDAnnotationKey
)

// KnownReasons is the operator-side view of the four tenant
// reasons. Sourced from shared/events at package init.
var KnownReasons = sharedevents.KnownReasons

// ShortReason is a re-export of sharedevents.ShortReason so
// operator-internal callers can consume it without pulling in the
// shared package alias.
func ShortReason(daemonReason string) string {
	return sharedevents.ShortReason(daemonReason)
}

// uidRE matches the "uid=<N>" fragment the daemon embeds in every
// event message. The pattern is anchored on the "uid=" prefix
// rather than the number alone so we don't false-match on stray
// digits (e.g. hostnames, port numbers) in a future message text
// that also happens to contain digits.
var uidRE = regexp.MustCompile(`\buid=(\d+)\b`)

// ParseUID extracts the principal UID from a daemon event message.
// Returns (uid, true) on success; (0, false) otherwise.
//
// This is the legacy path — kept exported for tests that only
// exercise the string. Production code should prefer
// ParseUIDFromEvent which reads the structured
// `kerberator.dell.com/uid` annotation the daemon stamps on
// every emitted Event.
func ParseUID(message string) (int64, bool) {
	m := uidRE.FindStringSubmatch(message)
	if m == nil {
		return 0, false
	}
	n, err := strconv.ParseInt(m[1], 10, 64)
	if err != nil {
		return 0, false
	}
	return n, true
}

// ParseUIDFromEvent extracts the principal UID from a daemon Event
// with a two-tier strategy:
//
//  1. Prefer the structured `kerberator.dell.com/uid` annotation.
//  2. Fall back to grep-parsing `uid=<N>` from Event.Message.
//
// A malformed annotation fails closed (returns (0, false)) rather
// than falling back to grep — a bogus annotation is a bug we want
// visible via the cache-miss code path.
func ParseUIDFromEvent(ev *corev1.Event) (int64, bool) {
	if ev == nil {
		return 0, false
	}
	if raw, ok := ev.Annotations[UIDAnnotationKey]; ok {
		if n, err := strconv.ParseInt(raw, 10, 64); err == nil {
			return n, true
		}
		return 0, false
	}
	return ParseUID(ev.Message)
}

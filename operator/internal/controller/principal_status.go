// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package controller

import (
	"sort"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
	"github.com/dell/kerberator/operator/internal/events"
)

// MinEventStaleness is the floor for how quickly a per-UID
// tenant Event can be declared stale. Rationale: on a tenant renewing every minute we still want events
// to be considered authoritative for at least 5 minutes,
// otherwise fallback dominates.
const MinEventStaleness = 5 * time.Minute

// ResolveStaleness computes the effective event-staleness
// threshold for one principal, honoring the Tenant spec override and
// the floor above.
//
// Precedence:
//
//  1. If tenant != nil and spec.eventStalenessThreshold > 0:
//     use that value verbatim.
//  2. Else if tenant != nil and tenant.spec.tenant.renewMinutes > 0:
//     use max(MinEventStaleness, 2 * renewMinutes).
//  3. Else: return MinEventStaleness. (Tenant renew interval
//     unknown — safe floor.)
func ResolveStaleness(tenant *v1alpha1.Tenant) time.Duration {
	if tenant != nil && tenant.Spec.EventStalenessThreshold != nil && tenant.Spec.EventStalenessThreshold.Duration > 0 {
		return tenant.Spec.EventStalenessThreshold.Duration
	}
	if tenant != nil && tenant.Spec.Daemon.RenewMinutes > 0 {
		derived := 2 * time.Duration(tenant.Spec.Daemon.RenewMinutes) * time.Minute
		if derived < MinEventStaleness {
			return MinEventStaleness
		}
		return derived
	}
	return MinEventStaleness
}

// BuildNodeStatuses produces the Principal.status.nodes[]
// slice for one principal by mixing per-UID event observations
// (from the events.Cache) with pod-readiness rollup (from
// TenantHealth).
//
// The result is deterministic (nodes sorted by name) and the
// caller can assign it verbatim to status.Nodes.
//
// Semantics per node:
//
//   - Cache hit + fresh (within staleness threshold): the
//     event's Reason (short-formed via events.ShortReason),
//     Message, and Timestamp populate the NodeStatus. The
//     DaemonPodReady bit still reflects the underlying pod's
//     readiness -- the "was the last mint attempt OK" answer
//     is orthogonal to "is the pod's overall probe passing",
//     though usually they agree.
//
//   - Cache hit but stale, or cache miss: fall back to the
//     pod-readiness data already in TenantHealth. NodeStatus
//     is copied through unchanged (unchanged).
//
//   - Pod has no name (unscheduled): skipped by TenantHealth
//     already; we never see it here.
//
// The (pod, uid) key is derived by pairing every tenant pod
// with the principal's uid. That works because kerberator-daemon
// emits Events on its own Pod's involvedObject, so the (pod,
// uid) space in the cache is exactly the space we're indexing
// here.
func BuildNodeStatuses(
	principal *v1alpha1.Principal,
	health TenantHealth,
	cache *events.Cache,
	now time.Time,
	staleness time.Duration,
) []v1alpha1.NodeStatus {
	if !health.Found {
		return nil
	}
	out := make([]v1alpha1.NodeStatus, 0, len(health.Nodes))
	// TenantHealth.Nodes is keyed by node name but each entry
	// was derived from a Pod. We don't preserve the pod name
	// in NodeStatus (it's an internal detail), so
	// health.Nodes[i].Name is the node name AND we need the
	// pod name separately for the cache lookup. That mapping
	// lives in TenantHealth.PodByNode (added below); when it
	// is not populated (e.g. tests that hand-build a
	// TenantHealth) we fall back to pod-readiness data
	// unchanged.
	for _, n := range health.Nodes {
		enriched := n
		if cache != nil && health.PodByNode != nil {
			if podName, ok := health.PodByNode[n.Name]; ok {
				if o, ok := cache.Lookup(podName, principal.Spec.UID); ok {
					if isFresh(o.Timestamp, now, staleness) {
						enriched.Reason = events.ShortReason(o.Reason)
						enriched.Message = o.Message
						enriched.LastObserved = metav1.NewTime(o.Timestamp)
					}
				}
			}
		}
		out = append(out, enriched)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// isFresh reports whether ts is within staleness of now. An
// observation with an exactly-equal timestamp counts as fresh
// so tests can drive the boundary deterministically.
func isFresh(ts, now time.Time, staleness time.Duration) bool {
	if ts.IsZero() {
		return false
	}
	return !ts.Before(now.Add(-staleness))
}

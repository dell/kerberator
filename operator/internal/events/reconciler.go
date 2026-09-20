// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package events

import (
	"context"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
)

// EventReconciler watches corev1.Event objects across the
// cluster (client.Client is namespace-agnostic; the manager
// scopes it via its own cache config) and ingests any event
// whose involvedObject.kind=Pod and whose reason is one of the
// four Kerberos* reasons defined in reasons.go.
//
// Filtering happens in two places:
//
//   - Predicate (Setup): rejects events whose Reason is not in
//     KnownReasons. Runs in the workqueue-enqueue path so
//     unrelated events never occupy a reconcile slot.
//
//   - Reconcile body: fetches the event and re-checks the kind
//
//   - reason. Necessary because a fast-updated event object
//     might have its reason mutated between predicate-time and
//     reconcile-time; and because in the pathological case an
//     event may have been deleted (fine: nothing to do).
//
// The reconciler writes ONLY to the in-memory Cache. It does
// not touch any Principal CR; the principal reconciler picks
// up cache updates on its next reconcile (which fires anyway
// because the principal reconciler also watches Events -- but see
// SetupPrincipalStatusWatch for that indirect wiring).
type EventReconciler struct {
	client.Client
	Cache *Cache
}

// SetupWithManager registers this reconciler on the manager. A
// predicate is attached that filters at enqueue-time: only
// Events whose Reason is in KnownReasons trigger a reconcile.
// This is a hot path (every K8s cluster generates many events);
// keeping non-matching events out of the workqueue is worth the
// predicate complexity.
func (r *EventReconciler) SetupWithManager(mgr ctrl.Manager) error {
	pred := predicate.NewPredicateFuncs(func(obj client.Object) bool {
		ev, ok := obj.(*corev1.Event)
		if !ok {
			return false
		}
		if ev.InvolvedObject.Kind != "Pod" {
			return false
		}
		_, want := KnownReasons[ev.Reason]
		return want
	})
	return ctrl.NewControllerManagedBy(mgr).
		Named("krb-event-ingester").
		For(&corev1.Event{}, builder.WithPredicates(pred, ignoreDeleteEvents{})).
		Complete(r)
}

// ignoreDeleteEvents is a small predicate that suppresses
// Delete events. Events are ephemeral (1h TTL by default in
// upstream apiserver) and their disappearance is not
// interesting to us -- Cache.GC handles staleness explicitly.
// Without this predicate we'd log one reconcile per
// event-object-expiration, which is noisy for zero benefit.
type ignoreDeleteEvents struct{ predicate.Funcs }

func (ignoreDeleteEvents) Delete(_ event.DeleteEvent) bool { return false }

// Reconcile ingests one event into the cache. Never returns an
// error that would trigger a requeue: event ingestion is
// best-effort and losing one event just means the next one
// (or the fallback pod-readiness path) will surface the truth.
func (r *EventReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues("event", req.NamespacedName)

	var ev corev1.Event
	err := r.Get(ctx, types.NamespacedName{Namespace: req.Namespace, Name: req.Name}, &ev)
	if apierrors.IsNotFound(err) {
		// Deleted between enqueue and reconcile. Nothing to do
		// -- GC handles cache eviction on its own timer.
		return ctrl.Result{}, nil
	}
	if err != nil {
		logger.Info("could not fetch event; will retry on next enqueue", "err", err.Error())
		return ctrl.Result{}, nil
	}

	// Re-check the predicate conditions. Cheap.
	if ev.InvolvedObject.Kind != "Pod" {
		return ctrl.Result{}, nil
	}
	if _, ok := KnownReasons[ev.Reason]; !ok {
		return ctrl.Result{}, nil
	}
	if ev.InvolvedObject.Name == "" {
		return ctrl.Result{}, nil
	}

	uid, ok := ParseUIDFromEvent(&ev)
	if !ok {
		// Tenant schema drift or a genuinely malformed event
		// (a human wrote it with kubectl create event). Log
		// once and drop.
		logger.Info("event with known reason missing uid=<N>; dropping",
			"reason", ev.Reason, "message", ev.Message)
		return ctrl.Result{}, nil
	}

	// Pick the event's most-informative timestamp. Both fields
	// can be zero-valued in synthetic events; if both are, fall
	// back to CreationTimestamp so the observation is at least
	// ordered relative to its arrival.
	ts := ev.LastTimestamp.Time
	if !ev.EventTime.IsZero() && ev.EventTime.After(ts) {
		ts = ev.EventTime.Time
	}
	if ts.IsZero() {
		ts = ev.CreationTimestamp.Time
	}

	r.Cache.Put(Observation{
		PodName:   ev.InvolvedObject.Name,
		UID:       uid,
		Reason:    ev.Reason,
		Message:   ev.Message,
		Timestamp: ts,
	})
	return ctrl.Result{}, nil
}

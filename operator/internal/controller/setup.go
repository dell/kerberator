// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// Package controller holds the Tenant and Principal reconcilers: the
// Tenant reconciler aggregates Principals into a per-realm DaemonSet,
// roster ConfigMap and keytab Secret; the Principal reconciler owns
// finalizers, per-node status enrichment and keytab KVNO observation.
package controller

import (
	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/predicate"

	krbevents "github.com/dell/kerberator/operator/internal/events"
)

// eventKnownReasonPredicate returns a predicate that admits only
// corev1.Event objects whose involvedObject.kind=Pod and whose
// reason is one of the tenant's Kerberos* reasons. Kept in the
// controller package (rather than events) to avoid an import
// cycle: events imports nothing from controller; controller
// depends on events for the cache type.
func eventKnownReasonPredicate() predicate.Predicate {
	return predicate.NewPredicateFuncs(func(obj client.Object) bool {
		ev, ok := obj.(*corev1.Event)
		if !ok {
			return false
		}
		if ev.InvolvedObject.Kind != "Pod" {
			return false
		}
		_, want := krbevents.KnownReasons[ev.Reason]
		return want
	})
}

// DaemonPodNameLabel is the constant value of app.kubernetes.io/name
// on kerberator-daemon Pods. Kept as an exported constant so the
// principal reconciler's Pod watcher and the tenant reconciler's Pod
// selector don't drift.
const DaemonPodNameLabel = "kerberator-daemon"

// DaemonPodNameLabelKey / DaemonPodInstanceLabelKey are the label
// keys the tenant reconciler stamps on every daemon Pod. The value
// of DaemonPodInstanceLabelKey is the parent Tenant's name.
const (
	DaemonPodNameLabelKey     = "app.kubernetes.io/name"
	DaemonPodInstanceLabelKey = "app.kubernetes.io/instance"
)

// isDaemonPod reports whether obj is a kerberator-daemon Pod.
func isDaemonPod(obj client.Object) bool {
	if _, ok := obj.(*corev1.Pod); !ok {
		return false
	}
	return obj.GetLabels()[DaemonPodNameLabelKey] == DaemonPodNameLabel
}

// podReadyStatus returns the Pod's Ready condition status, or
// corev1.ConditionUnknown if the Pod has no Ready condition yet.
// Distinct from tenant_status.go's podReady() helper (which returns
// (bool, humanReason) for status projection); this one is the
// three-valued form the predicate needs for transition detection.
func podReadyStatus(p *corev1.Pod) corev1.ConditionStatus {
	if p == nil {
		return corev1.ConditionUnknown
	}
	for _, c := range p.Status.Conditions {
		if c.Type == corev1.PodReady {
			return c.Status
		}
	}
	return corev1.ConditionUnknown
}

// daemonPodReadyTransitionPredicate returns a predicate that admits
// only corev1.Pod events that could affect a Principal's derived
// daemonPodReady bit for the parent Tenant:
//
//   - Create: the DaemonSet just scheduled a new daemon Pod. The
//     new Pod's Ready is almost certainly false-transitioning-to-
//     true; enqueue so the principal re-observes.
//   - Update: only if the Ready condition status actually changed.
//     Filters out the noisy per-second kubelet status heartbeats
//     that don't move Ready.
//   - Delete: the daemon Pod went away. Enqueue so the principal
//     status reflects the (now-)missing pod on that node.
//   - Generic: nothing we care about; skip.
//
// The `isDaemonPod` gate on every branch means non-daemon Pods
// never enter the enqueue path, so this predicate is safe to use
// with a cluster-scope Pod watch on the operator.
func daemonPodReadyTransitionPredicate() predicate.Predicate {
	return predicate.Funcs{
		CreateFunc: func(e event.CreateEvent) bool {
			return isDaemonPod(e.Object)
		},
		UpdateFunc: func(e event.UpdateEvent) bool {
			if !isDaemonPod(e.ObjectNew) {
				return false
			}
			oldPod, okOld := e.ObjectOld.(*corev1.Pod)
			newPod, okNew := e.ObjectNew.(*corev1.Pod)
			if !okOld || !okNew {
				return false
			}
			return podReadyStatus(oldPod) != podReadyStatus(newPod)
		},
		DeleteFunc: func(e event.DeleteEvent) bool {
			return isDaemonPod(e.Object)
		},
		GenericFunc: func(_ event.GenericEvent) bool {
			return false
		},
	}
}

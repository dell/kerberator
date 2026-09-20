// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package controller

import (
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/event"
)

// daemonPod builds a Pod with the operator-stamped daemon labels
// and the given Ready condition status. Used by the predicate
// tests below to keep the intent visible per case.
func daemonPod(name string, ready corev1.ConditionStatus) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: "ns",
			Labels: map[string]string{
				DaemonPodNameLabelKey:     DaemonPodNameLabel,
				DaemonPodInstanceLabelKey: "tenant-a",
			},
		},
		Status: corev1.PodStatus{
			Conditions: []corev1.PodCondition{
				{Type: corev1.PodReady, Status: ready},
			},
		},
	}
}

func nonDaemonPod(name string) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: "ns",
			Labels:    map[string]string{DaemonPodNameLabelKey: "some-other-app"},
		},
	}
}

func TestDaemonPodReadyTransitionPredicate_Create(t *testing.T) {
	p := daemonPodReadyTransitionPredicate()

	if !p.Create(event.CreateEvent{Object: daemonPod("a", corev1.ConditionFalse)}) {
		t.Error("expected Create on daemon Pod to enqueue")
	}
	if p.Create(event.CreateEvent{Object: nonDaemonPod("b")}) {
		t.Error("Create on non-daemon Pod should not enqueue")
	}
}

func TestDaemonPodReadyTransitionPredicate_Update_ReadyChanged(t *testing.T) {
	p := daemonPodReadyTransitionPredicate()

	oldP := daemonPod("a", corev1.ConditionFalse)
	newP := daemonPod("a", corev1.ConditionTrue)
	if !p.Update(event.UpdateEvent{ObjectOld: oldP, ObjectNew: newP}) {
		t.Error("Update with Ready False->True should enqueue")
	}

	oldP = daemonPod("a", corev1.ConditionTrue)
	newP = daemonPod("a", corev1.ConditionTrue)
	if p.Update(event.UpdateEvent{ObjectOld: oldP, ObjectNew: newP}) {
		t.Error("Update with unchanged Ready should NOT enqueue (noisy heartbeats must be filtered)")
	}

	if p.Update(event.UpdateEvent{ObjectOld: nonDaemonPod("b"), ObjectNew: nonDaemonPod("b")}) {
		t.Error("Update on non-daemon Pod must not enqueue")
	}
}

func TestDaemonPodReadyTransitionPredicate_Delete(t *testing.T) {
	p := daemonPodReadyTransitionPredicate()

	if !p.Delete(event.DeleteEvent{Object: daemonPod("a", corev1.ConditionTrue)}) {
		t.Error("Delete of daemon Pod should enqueue (missing pod affects principal status)")
	}
	if p.Delete(event.DeleteEvent{Object: nonDaemonPod("b")}) {
		t.Error("Delete of non-daemon Pod must not enqueue")
	}
}

func TestPodReadyStatus_Absent(t *testing.T) {
	// No Ready condition at all — Pod hasn't been scheduled yet or
	// kubelet hasn't reported. We treat this as Unknown so the
	// predicate's Update path can distinguish it from ConditionTrue.
	p := &corev1.Pod{}
	if got := podReadyStatus(p); got != corev1.ConditionUnknown {
		t.Errorf("podReadyStatus(no conditions) = %v, want Unknown", got)
	}
	if got := podReadyStatus(nil); got != corev1.ConditionUnknown {
		t.Errorf("podReadyStatus(nil) = %v, want Unknown", got)
	}
}

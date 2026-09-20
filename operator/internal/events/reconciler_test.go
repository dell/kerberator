// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package events

import (
	"context"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func newReconciler(objs ...runtime.Object) (*EventReconciler, *Cache) {
	scheme := runtime.NewScheme()
	_ = corev1.AddToScheme(scheme)
	c := NewCache(time.Hour)
	fc := fake.NewClientBuilder().WithScheme(scheme).WithRuntimeObjects(objs...).Build()
	return &EventReconciler{Client: fc, Cache: c}, c
}

func mkEvent(name, ns, reason, msg, podName string, ts time.Time) *corev1.Event {
	return &corev1.Event{
		ObjectMeta:     metav1.ObjectMeta{Name: name, Namespace: ns},
		InvolvedObject: corev1.ObjectReference{Kind: "Pod", Name: podName, Namespace: ns},
		Reason:         reason,
		Message:        msg,
		LastTimestamp:  metav1.NewTime(ts),
	}
}

func TestEventReconciler_IngestsKnownReason(t *testing.T) {
	ts := time.Now().Truncate(time.Second)
	ev := mkEvent("ev1", "krb", ReasonMintSucceeded, "uid=1000 minted for p@R", "tenant-pod-1", ts)
	r, c := newReconciler(ev)
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "krb", Name: "ev1"}}); err != nil {
		t.Fatal(err)
	}
	got, ok := c.Lookup("tenant-pod-1", 1000)
	if !ok {
		t.Fatal("expected cache hit after reconcile")
	}
	if got.Reason != ReasonMintSucceeded || !got.Timestamp.Equal(ts) {
		t.Errorf("bad observation: %+v (want ts=%s)", got, ts)
	}
}

func TestEventReconciler_IgnoresNonPodInvolvedObject(t *testing.T) {
	ts := time.Now()
	ev := mkEvent("ev", "krb", ReasonMintFailed, "uid=1 foo", "", ts)
	ev.InvolvedObject.Kind = "Deployment"
	ev.InvolvedObject.Name = "d1"
	r, c := newReconciler(ev)
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "krb", Name: "ev"}}); err != nil {
		t.Fatal(err)
	}
	if c.Len() != 0 {
		t.Errorf("expected empty cache, got Len=%d", c.Len())
	}
}

func TestEventReconciler_DropsMalformedMessage(t *testing.T) {
	ev := mkEvent("ev", "krb", ReasonMintSucceeded, "minted but no uid", "p1", time.Now())
	r, c := newReconciler(ev)
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "krb", Name: "ev"}}); err != nil {
		t.Fatal(err)
	}
	if c.Len() != 0 {
		t.Errorf("expected empty cache, got Len=%d", c.Len())
	}
}

func TestEventReconciler_MissingEventNoError(t *testing.T) {
	r, c := newReconciler()
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "krb", Name: "gone"}}); err != nil {
		t.Fatal(err)
	}
	if c.Len() != 0 {
		t.Fatal("cache mutated on missing event")
	}
}

func TestEventReconciler_UnknownReasonRejected(t *testing.T) {
	// Bypasses the predicate (which fires at enqueue time); the
	// body still guards on KnownReasons.
	ev := mkEvent("ev", "krb", "SomeOtherReason", "uid=1 hi", "p1", time.Now())
	r, c := newReconciler(ev)
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "krb", Name: "ev"}}); err != nil {
		t.Fatal(err)
	}
	if c.Len() != 0 {
		t.Errorf("expected empty cache, got Len=%d", c.Len())
	}
}

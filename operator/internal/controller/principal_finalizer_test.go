// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package controller

import (
	"context"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

const (
	finNS   = "kt-fin"
	finKCF  = "kcf"
	finName = "uid-2001"
	finUID  = int64(2001)
)

func newFinalizerScheme() *runtime.Scheme {
	s := runtime.NewScheme()
	_ = clientgoscheme.AddToScheme(s)
	_ = v1alpha1.AddToScheme(s)
	return s
}

func newPrincipalReconciler(now time.Time, objs ...runtime.Object) *PrincipalReconciler {
	s := newFinalizerScheme()
	cl := fake.NewClientBuilder().
		WithScheme(s).
		WithStatusSubresource(&v1alpha1.Principal{}, &v1alpha1.Tenant{}).
		WithRuntimeObjects(objs...).
		Build()
	return &PrincipalReconciler{
		Client:   cl,
		Recorder: &record.FakeRecorder{Events: make(chan string, 32)},
		Now:      func() time.Time { return now },
	}
}

func mkManagePrincipal(delTS *metav1.Time, finalizers ...string) *v1alpha1.Principal {
	kt := &v1alpha1.Principal{
		ObjectMeta: metav1.ObjectMeta{
			Name:      finName,
			Namespace: finNS,
			// fake client only respects DeletionTimestamp when a
			// finalizer is present, otherwise it just deletes the
			// object.
			Finalizers: append([]string{}, finalizers...),
		},
		Spec: v1alpha1.PrincipalSpec{
			UID:       finUID,
			Principal: "svc-a@TENANT",
			TenantRef: v1alpha1.TenantRef{
				Name: finKCF,
			},
			KeytabSecretRef: &v1alpha1.SecretKeyRef{
				Name: "keytab-2001",
				Key:  "svc-a.keytab",
			},
		},
	}
	if delTS != nil {
		kt.DeletionTimestamp = delTS
	}
	return kt
}

func mkKCFForFinalizer(timeout *time.Duration) *v1alpha1.Tenant {
	kcf := &v1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: finKCF, Namespace: finNS},
		Spec: v1alpha1.TenantSpec{
			Realm: "REALM",
		},
	}
	if timeout != nil {
		kcf.Spec.DeletionTimeout = &metav1.Duration{Duration: *timeout}
	}
	return kcf
}

func mkFactoryDS(desired int32) *appsv1.DaemonSet {
	return &appsv1.DaemonSet{
		ObjectMeta: metav1.ObjectMeta{Name: finKCF + "-kerberator-daemon", Namespace: finNS},
		Spec: appsv1.DaemonSetSpec{
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "kcf"}},
		},
		Status: appsv1.DaemonSetStatus{DesiredNumberScheduled: desired},
	}
}

func mkPurgeEvent(podName string, uid int64, when metav1.Time) *corev1.Event {
	return &corev1.Event{
		ObjectMeta: metav1.ObjectMeta{
			Name:      podName + "-purge",
			Namespace: finNS,
		},
		InvolvedObject: corev1.ObjectReference{
			Kind:      "Pod",
			Name:      podName,
			Namespace: finNS,
		},
		Reason:        PurgeEventReason,
		Message:       "removed stale ccache for uid=" + itoa(uid),
		LastTimestamp: when,
	}
}

func itoa(n int64) string {
	if n == 0 {
		return "0"
	}
	buf := [20]byte{}
	i := len(buf)
	neg := n < 0
	if neg {
		n = -n
	}
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}

// --- Path A: not being deleted ---

func TestPrincipalReconcile_AddsFinalizer(t *testing.T) {
	now := time.Now()
	kt := mkManagePrincipal(nil)
	kcf := mkKCFForFinalizer(nil)
	r := newPrincipalReconciler(now, kt, kcf)

	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: finNS, Name: finName}}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var got v1alpha1.Principal
	if err := r.Get(context.Background(), req.NamespacedName, &got); err != nil {
		t.Fatalf("get: %v", err)
	}
	if !hasFinalizer(&got, PrincipalFinalizer) {
		t.Errorf("expected finalizer %q, got %v", PrincipalFinalizer, got.Finalizers)
	}
}

// --- Path B: being deleted ---

func TestPrincipalReconcile_DeleteRemovesFinalizerWhenNoDesiredNodes(t *testing.T) {
	// DS desiredNodes=0 => nothing to confirm => remove immediately.
	now := time.Now()
	delTS := metav1.NewTime(now.Add(-1 * time.Second))
	kt := mkManagePrincipal(&delTS, PrincipalFinalizer)
	kcf := mkKCFForFinalizer(nil)
	ds := mkFactoryDS(0)
	r := newPrincipalReconciler(now, kt, kcf, ds)
	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: finNS, Name: finName}}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var got v1alpha1.Principal
	err := r.Get(context.Background(), req.NamespacedName, &got)
	// Either the object is gone (fake client honored deletionTimestamp+empty
	// finalizers by removing it) or the finalizer is stripped.
	if err == nil && hasFinalizer(&got, PrincipalFinalizer) {
		t.Errorf("expected finalizer removed, got: %v", got.Finalizers)
	}
}

func TestPrincipalReconcile_DeleteRemovesFinalizerAfterAllPurgeEvents(t *testing.T) {
	now := time.Now()
	delTS := metav1.NewTime(now.Add(-2 * time.Second))
	kt := mkManagePrincipal(&delTS, PrincipalFinalizer)
	kcf := mkKCFForFinalizer(nil)
	ds := mkFactoryDS(2)
	ev1 := mkPurgeEvent("pod-1", finUID, metav1.NewTime(now.Add(-1*time.Second)))
	ev2 := mkPurgeEvent("pod-2", finUID, metav1.NewTime(now.Add(-1*time.Second)))
	r := newPrincipalReconciler(now, kt, kcf, ds, ev1, ev2)
	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: finNS, Name: finName}}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var got v1alpha1.Principal
	err := r.Get(context.Background(), req.NamespacedName, &got)
	if err == nil && hasFinalizer(&got, PrincipalFinalizer) {
		t.Errorf("expected finalizer removed after purge events, got: %v", got.Finalizers)
	}
}

func TestPrincipalReconcile_DeleteWaitsForRemainingNodes(t *testing.T) {
	// 2 desired nodes but only 1 purge event -- should NOT remove finalizer yet.
	now := time.Now()
	delTS := metav1.NewTime(now.Add(-2 * time.Second))
	kt := mkManagePrincipal(&delTS, PrincipalFinalizer)
	kcf := mkKCFForFinalizer(nil)
	ds := mkFactoryDS(2)
	ev1 := mkPurgeEvent("pod-1", finUID, metav1.NewTime(now.Add(-1*time.Second)))
	r := newPrincipalReconciler(now, kt, kcf, ds, ev1)
	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: finNS, Name: finName}}
	res, err := r.Reconcile(context.Background(), req)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if res.RequeueAfter == 0 {
		t.Errorf("expected non-zero requeue while waiting for purge, got %+v", res)
	}
	var got v1alpha1.Principal
	if err := r.Get(context.Background(), req.NamespacedName, &got); err != nil {
		t.Fatalf("get: %v", err)
	}
	if !hasFinalizer(&got, PrincipalFinalizer) {
		t.Errorf("finalizer stripped prematurely: %v", got.Finalizers)
	}
}

func TestPrincipalReconcile_DeleteRemovesFinalizerOnTimeout(t *testing.T) {
	// timeout=1s, deletionTimestamp 5s ago, no purge events => strip on timeout.
	now := time.Now()
	delTS := metav1.NewTime(now.Add(-5 * time.Second))
	timeout := 1 * time.Second
	kt := mkManagePrincipal(&delTS, PrincipalFinalizer)
	kcf := mkKCFForFinalizer(&timeout)
	ds := mkFactoryDS(2)
	r := newPrincipalReconciler(now, kt, kcf, ds)
	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: finNS, Name: finName}}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var got v1alpha1.Principal
	err := r.Get(context.Background(), req.NamespacedName, &got)
	if err == nil && hasFinalizer(&got, PrincipalFinalizer) {
		t.Errorf("expected finalizer stripped on timeout: %v", got.Finalizers)
	}
	// Verify a CcachePurgeTimeout event was recorded.
	rec := r.Recorder.(*record.FakeRecorder)
	select {
	case ev := <-rec.Events:
		if !contains(ev, "CcachePurgeTimeout") {
			t.Errorf("expected CcachePurgeTimeout event, got %q", ev)
		}
	default:
		t.Errorf("no event recorded on timeout")
	}
}

func TestPrincipalReconcile_DeleteRemovesFinalizerWhenFactoryGone(t *testing.T) {
	now := time.Now()
	delTS := metav1.NewTime(now.Add(-1 * time.Second))
	kt := mkManagePrincipal(&delTS, PrincipalFinalizer)
	// no KCF in cluster
	r := newPrincipalReconciler(now, kt)
	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: finNS, Name: finName}}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var got v1alpha1.Principal
	err := r.Get(context.Background(), req.NamespacedName, &got)
	if err == nil && hasFinalizer(&got, PrincipalFinalizer) {
		t.Errorf("expected finalizer stripped when tenant gone: %v", got.Finalizers)
	}
}

func TestPrincipalReconcile_IgnoresPurgeEventBeforeDeletion(t *testing.T) {
	// Purge event with timestamp BEFORE deletion started -- must not
	// count. Otherwise a stale event from a prior lifecycle of the
	// same UID would falsely confirm.
	now := time.Now()
	delTS := metav1.NewTime(now.Add(-1 * time.Second))
	kt := mkManagePrincipal(&delTS, PrincipalFinalizer)
	kcf := mkKCFForFinalizer(nil)
	ds := mkFactoryDS(1)
	stale := mkPurgeEvent("pod-1", finUID, metav1.NewTime(now.Add(-10*time.Second)))
	r := newPrincipalReconciler(now, kt, kcf, ds, stale)
	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: finNS, Name: finName}}
	res, err := r.Reconcile(context.Background(), req)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if res.RequeueAfter == 0 {
		t.Errorf("expected requeue (stale event should not count), got %+v", res)
	}
	var got v1alpha1.Principal
	if err := r.Get(context.Background(), req.NamespacedName, &got); err != nil {
		t.Fatalf("get: %v", err)
	}
	if !hasFinalizer(&got, PrincipalFinalizer) {
		t.Errorf("finalizer stripped by stale event: %v", got.Finalizers)
	}
}

func contains(haystack, needle string) bool {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}

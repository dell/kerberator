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
	factoryTestNS   = "kcf-test"
	factoryTestName = "kcf"
)

// hasCondition scans a []metav1.Condition for one matching (type,
// status). Shared by every reconciler test in this package.
func hasCondition(conds []metav1.Condition, condType string, status metav1.ConditionStatus) bool {
	for _, c := range conds {
		if c.Type == condType && c.Status == status {
			return true
		}
	}
	return false
}

func newFactoryScheme() *runtime.Scheme {
	s := runtime.NewScheme()
	_ = clientgoscheme.AddToScheme(s)
	_ = v1alpha1.AddToScheme(s)
	return s
}

func newTenantReconciler(objs ...runtime.Object) *TenantReconciler {
	s := newFactoryScheme()
	cl := fake.NewClientBuilder().
		WithScheme(s).
		WithStatusSubresource(&v1alpha1.Tenant{}, &v1alpha1.Principal{}).
		WithRuntimeObjects(objs...).
		Build()
	return &TenantReconciler{
		Client:          cl,
		Scheme:          s,
		Recorder:        &record.FakeRecorder{Events: make(chan string, 32)},
		RequeueInterval: 15 * time.Second,
	}
}

func mkKCF() *v1alpha1.Tenant {
	return &v1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{
			Name:      factoryTestName,
			Namespace: factoryTestNS,
			UID:       "abc-123",
		},
		Spec: v1alpha1.TenantSpec{
			Realm: "EXAMPLE.COM",
		},
	}
}

func mkKT(name string, uid int64, kcfName, secretName, secretKey string) *v1alpha1.Principal {
	return &v1alpha1.Principal{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: factoryTestNS},
		Spec: v1alpha1.PrincipalSpec{
			UID:       uid,
			Principal: name + "@EXAMPLE.COM",
			TenantRef: v1alpha1.TenantRef{
				Name: kcfName,
			},
			KeytabSecretRef: &v1alpha1.SecretKeyRef{
				Name: secretName,
				Key:  secretKey,
			},
		},
	}
}

func mkKeytabSecret(name string, data map[string][]byte) *corev1.Secret {
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: factoryTestNS},
		Type:       corev1.SecretTypeOpaque,
		Data:       data,
	}
}

func TestFactoryReconcile_CreatesChildren(t *testing.T) {
	kcf := mkKCF()
	kt1 := mkKT("uid-2001", 2001, factoryTestName, "keytab-2001", "svc-a.keytab")
	kt2 := mkKT("uid-2002", 2002, factoryTestName, "keytab-2002", "svc-b.keytab")
	sec1 := mkKeytabSecret("keytab-2001", map[string][]byte{"svc-a.keytab": []byte("A")})
	sec2 := mkKeytabSecret("keytab-2002", map[string][]byte{"svc-b.keytab": []byte("BB")})

	r := newTenantReconciler(kcf, kt1, kt2, sec1, sec2)
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName}}); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	// Roster CM.
	var cm corev1.ConfigMap
	if err := r.Get(context.Background(), types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName + "-roster"}, &cm); err != nil {
		t.Fatalf("get roster cm: %v", err)
	}
	got := cm.Data["users.roster"]
	want := "2001:uid-2001@EXAMPLE.COM:svc-a.keytab\n2002:uid-2002@EXAMPLE.COM:svc-b.keytab\n"
	if got != want {
		t.Errorf("roster mismatch:\ngot:  %q\nwant: %q", got, want)
	}
	if !ownedBy(&cm, kcf) {
		t.Errorf("roster CM missing controller ownerRef to kcf: %+v", cm.OwnerReferences)
	}

	// Keytab Secret.
	var sec corev1.Secret
	if err := r.Get(context.Background(), types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName + "-keytabs"}, &sec); err != nil {
		t.Fatalf("get keytab secret: %v", err)
	}
	if string(sec.Data["svc-a.keytab"]) != "A" || string(sec.Data["svc-b.keytab"]) != "BB" {
		t.Errorf("keytab data wrong: %v", sec.Data)
	}
	if !ownedBy(&sec, kcf) {
		t.Errorf("keytab secret missing controller ownerRef")
	}

	// DaemonSet.
	var ds appsv1.DaemonSet
	if err := r.Get(context.Background(), types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName + "-kerberator-daemon"}, &ds); err != nil {
		t.Fatalf("get ds: %v", err)
	}
	if !ownedBy(&ds, kcf) {
		t.Errorf("DS missing controller ownerRef")
	}

	// Status.
	var gotKcf v1alpha1.Tenant
	if err := r.Get(context.Background(), types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName}, &gotKcf); err != nil {
		t.Fatalf("get kcf: %v", err)
	}
	if gotKcf.Status.PrincipalCount != 2 {
		t.Errorf("expected PrincipalCount=2, got %d", gotKcf.Status.PrincipalCount)
	}
	if gotKcf.Status.RosterHash == "" {
		t.Errorf("expected non-empty RosterHash")
	}
	if !hasCondition(gotKcf.Status.Conditions, "RosterConsistent", metav1.ConditionTrue) {
		t.Errorf("expected RosterConsistent=True, got %+v", gotKcf.Status.Conditions)
	}
	if !hasCondition(gotKcf.Status.Conditions, "KeytabsResolved", metav1.ConditionTrue) {
		t.Errorf("expected KeytabsResolved=True, got %+v", gotKcf.Status.Conditions)
	}
}

func TestFactoryReconcile_DuplicateUIDRejectsBoth(t *testing.T) {
	kcf := mkKCF()
	kt1 := mkKT("uid-2001-a", 2001, factoryTestName, "keytab-a", "a.keytab")
	kt2 := mkKT("uid-2001-b", 2001, factoryTestName, "keytab-b", "b.keytab")
	sec1 := mkKeytabSecret("keytab-a", map[string][]byte{"a.keytab": []byte("A")})
	sec2 := mkKeytabSecret("keytab-b", map[string][]byte{"b.keytab": []byte("B")})

	r := newTenantReconciler(kcf, kt1, kt2, sec1, sec2)
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName}}); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var cm corev1.ConfigMap
	if err := r.Get(context.Background(), types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName + "-roster"}, &cm); err != nil {
		t.Fatalf("get cm: %v", err)
	}
	if cm.Data["users.roster"] != "" {
		t.Errorf("expected empty roster on dup, got %q", cm.Data["users.roster"])
	}
	var gotKcf v1alpha1.Tenant
	_ = r.Get(context.Background(), types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName}, &gotKcf)
	if !hasCondition(gotKcf.Status.Conditions, "RosterConsistent", metav1.ConditionFalse) {
		t.Errorf("expected RosterConsistent=False, got %+v", gotKcf.Status.Conditions)
	}
}

func TestFactoryReconcile_MissingKeytabSurfaces(t *testing.T) {
	kcf := mkKCF()
	kt := mkKT("uid-2001", 2001, factoryTestName, "keytab-missing", "svc.keytab")
	// No secret created.
	r := newTenantReconciler(kcf, kt)
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName}}); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var gotKcf v1alpha1.Tenant
	_ = r.Get(context.Background(), types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName}, &gotKcf)
	if !hasCondition(gotKcf.Status.Conditions, "KeytabsResolved", metav1.ConditionFalse) {
		t.Errorf("expected KeytabsResolved=False, got %+v", gotKcf.Status.Conditions)
	}
}

func TestFactoryReconcile_RefusesToClobberUnownedCM(t *testing.T) {
	// A pre-existing CM with the same name but no ownerRef to
	// kcf must NOT be mutated. This is the safety guard.
	kcf := mkKCF()
	preCM := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: factoryTestNS,
			Name:      factoryTestName + "-roster",
		},
		Data: map[string]string{"users.roster": "user-authored-content\n"},
	}
	r := newTenantReconciler(kcf, preCM)
	_, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName}})
	if err == nil {
		t.Fatalf("expected reconcile to fail on unowned CM, got nil")
	}
	var after corev1.ConfigMap
	if err := r.Get(context.Background(), types.NamespacedName{Namespace: factoryTestNS, Name: preCM.Name}, &after); err != nil {
		t.Fatalf("get cm: %v", err)
	}
	if after.Data["users.roster"] != "user-authored-content\n" {
		t.Errorf("reconciler clobbered user-authored CM: %q", after.Data["users.roster"])
	}
}

func TestFactoryReconcile_DeletingPrincipalExcludedFromRoster(t *testing.T) {
	// A principal with a deletionTimestamp must be dropped from the
	// aggregation immediately, even though it hasn't finished
	// disappearing from the API yet. The principal finalizer waits
	// for on-node purge separately.
	kcf := mkKCF()
	now := metav1.Now()
	deleting := mkKT("uid-2001", 2001, factoryTestName, "keytab-2001", "svc-a.keytab")
	deleting.Finalizers = []string{PrincipalFinalizer}
	deleting.DeletionTimestamp = &now
	alive := mkKT("uid-2002", 2002, factoryTestName, "keytab-2002", "svc-b.keytab")
	sec1 := mkKeytabSecret("keytab-2001", map[string][]byte{"svc-a.keytab": []byte("A")})
	sec2 := mkKeytabSecret("keytab-2002", map[string][]byte{"svc-b.keytab": []byte("BB")})

	r := newTenantReconciler(kcf, deleting, alive, sec1, sec2)
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName}}); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var cm corev1.ConfigMap
	if err := r.Get(context.Background(), types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName + "-roster"}, &cm); err != nil {
		t.Fatalf("get cm: %v", err)
	}
	got := cm.Data["users.roster"]
	// Only uid-2002 should appear.
	if got != "2002:uid-2002@EXAMPLE.COM:svc-b.keytab\n" {
		t.Errorf("expected only surviving principal in roster, got %q", got)
	}
}

func TestFactoryReconcile_PrincipalsInOtherFactoryIgnored(t *testing.T) {
	kcf := mkKCF()
	// This principal references a different tenant.
	kt := mkKT("uid-2001", 2001, "other-tenant", "keytab-x", "x.keytab")
	sec := mkKeytabSecret("keytab-x", map[string][]byte{"x.keytab": []byte("X")})
	r := newTenantReconciler(kcf, kt, sec)
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName}}); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var cm corev1.ConfigMap
	if err := r.Get(context.Background(), types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName + "-roster"}, &cm); err != nil {
		t.Fatalf("get cm: %v", err)
	}
	if cm.Data["users.roster"] != "" {
		t.Errorf("expected empty roster (no matching principals), got %q", cm.Data["users.roster"])
	}
	var gotKcf v1alpha1.Tenant
	_ = r.Get(context.Background(), types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName}, &gotKcf)
	if gotKcf.Status.PrincipalCount != 0 {
		t.Errorf("expected PrincipalCount=0, got %d", gotKcf.Status.PrincipalCount)
	}
}

func TestFactoryReconcile_DisabledPrincipalExcludedFromRoster(t *testing.T) {
	// v0.7.0 feature: a Principal with spec.disabled=true is excluded
	// from the aggregated roster, but is STILL counted in
	// status.PrincipalCount (so `kubectl get tenant` accurately reflects
	// how many Principals exist under this Tenant).
	kcf := mkKCF()
	disabled := mkKT("uid-2001", 2001, factoryTestName, "keytab-2001", "svc-a.keytab")
	disabled.Spec.Disabled = true
	disabled.Spec.DisabledReason = "compromised"
	alive := mkKT("uid-2002", 2002, factoryTestName, "keytab-2002", "svc-b.keytab")
	sec1 := mkKeytabSecret("keytab-2001", map[string][]byte{"svc-a.keytab": []byte("A")})
	sec2 := mkKeytabSecret("keytab-2002", map[string][]byte{"svc-b.keytab": []byte("BB")})

	r := newTenantReconciler(kcf, disabled, alive, sec1, sec2)
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName}}); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var cm corev1.ConfigMap
	if err := r.Get(context.Background(), types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName + "-roster"}, &cm); err != nil {
		t.Fatalf("get cm: %v", err)
	}
	if cm.Data["users.roster"] != "2002:uid-2002@EXAMPLE.COM:svc-b.keytab\n" {
		t.Errorf("expected only enabled principal in roster, got %q", cm.Data["users.roster"])
	}
	var got v1alpha1.Tenant
	_ = r.Get(context.Background(), types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName}, &got)
	if got.Status.PrincipalCount != 2 {
		t.Errorf("expected PrincipalCount=2 (disabled principals still count for status), got %d", got.Status.PrincipalCount)
	}
}

func TestFactoryReconcile_DisabledTenantProducesEmptyRoster(t *testing.T) {
	// v0.7.0 feature: a Tenant with spec.disabled=true aggregates
	// NO principals into its roster even if they exist and are
	// individually enabled. This is the "whole-tenant kill switch"
	// primitive — DaemonSet keeps running (ownerRef graph is
	// preserved), but the roster is empty so the daemon prunes
	// everything on its next poll.
	kcf := mkKCF()
	kcf.Spec.Disabled = true
	// Two individually-enabled principals that WOULD be in the roster
	// if the tenant itself weren't disabled.
	t1 := mkKT("uid-2001", 2001, factoryTestName, "keytab-2001", "svc-a.keytab")
	t2 := mkKT("uid-2002", 2002, factoryTestName, "keytab-2002", "svc-b.keytab")
	sec1 := mkKeytabSecret("keytab-2001", map[string][]byte{"svc-a.keytab": []byte("A")})
	sec2 := mkKeytabSecret("keytab-2002", map[string][]byte{"svc-b.keytab": []byte("BB")})

	r := newTenantReconciler(kcf, t1, t2, sec1, sec2)
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName}}); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var cm corev1.ConfigMap
	if err := r.Get(context.Background(), types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName + "-roster"}, &cm); err != nil {
		t.Fatalf("get cm: %v", err)
	}
	if cm.Data["users.roster"] != "" {
		t.Errorf("expected empty roster (tenant disabled), got %q", cm.Data["users.roster"])
	}
	var got v1alpha1.Tenant
	_ = r.Get(context.Background(), types.NamespacedName{Namespace: factoryTestNS, Name: factoryTestName}, &got)
	if got.Status.PrincipalCount != 2 {
		t.Errorf("expected PrincipalCount=2 (principals still exist even if tenant-disabled), got %d", got.Status.PrincipalCount)
	}
}

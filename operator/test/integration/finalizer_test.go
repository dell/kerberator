// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package integration

import (
	"fmt"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
	"github.com/dell/kerberator/operator/internal/controller"
)

// TestPrincipalFinalizerLifecycle exercises the manage-mode
// Principal finalizer end-to-end:
//
//  1. On create, the reconciler adds the
//     `kerberator.dell.com/ccache-purged` finalizer.
//  2. On delete, when DesiredNodes == 0 (no tenant pods scheduled,
//     which is the case in envtest since we don't run kubelet),
//     the finalizer is removed immediately per principal_finalizer.go's
//     "nothing to purge" branch — the principal transitions to gone
//     without any timeout.
//
// The two other paths (per-node event-driven purge confirmation, and
// the deletionTimeout fallback) are unit-tested with a fake clock in
// principal_finalizer_test.go; here we prove the plumbing is real.
func TestPrincipalFinalizerLifecycle(t *testing.T) {
	nsName := "fin-ns"
	mustCreateNS(t, nsName, "2001/1")

	kcfName := "fin-kcf"
	if err := k8sClient.Create(testCtx, &v1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: kcfName, Namespace: nsName},
		Spec: v1alpha1.TenantSpec{
			Realm: "EXAMPLE.COM",
			Daemon: v1alpha1.DaemonTemplate{
				Image: "ghcr.io/dell/kerberator-daemon:v0.3.0",
			},
			// Short so the timeout branch is testable within a
			// tight envtest budget if we ever exercise it.
			DeletionTimeout: &metav1.Duration{Duration: 5 * time.Second},
		},
	}); err != nil {
		t.Fatalf("create kcf: %v", err)
	}

	if err := k8sClient.Create(testCtx, &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: "fin-kt", Namespace: nsName},
		Data:       map[string][]byte{"svc.keytab": []byte("KT")},
	}); err != nil {
		t.Fatalf("create secret: %v", err)
	}

	principalName := "fin-principal"
	principal := &v1alpha1.Principal{
		ObjectMeta: metav1.ObjectMeta{Name: principalName, Namespace: nsName},
		Spec: v1alpha1.PrincipalSpec{
			UID:             2001,
			Principal:       "svc-2001@EXAMPLE.COM",
			Keytab:          "svc.keytab",
			TenantRef:       v1alpha1.TenantRef{Name: kcfName},
			KeytabSecretRef: &v1alpha1.SecretKeyRef{Name: "fin-kt", Key: "svc.keytab"},
		},
	}
	if err := k8sClient.Create(testCtx, principal); err != nil {
		t.Fatalf("create principal: %v", err)
	}

	// Step 1: finalizer is stamped on the principal by the reconciler.
	eventually(t, 20*time.Second, func() error {
		var got v1alpha1.Principal
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: nsName, Name: principalName}, &got); err != nil {
			return err
		}
		for _, f := range got.Finalizers {
			if f == controller.PrincipalFinalizer {
				return nil
			}
		}
		return &notReadyErr{msg: "finalizer not yet added: " + strings.Join(got.Finalizers, ",")}
	})

	// Step 2: delete the principal. Since DesiredNodes==0 in envtest
	// (no kubelet, no scheduled DS pods), the finalizer's "nothing
	// to purge" branch should fire and the object should disappear.
	if err := k8sClient.Delete(testCtx, principal); err != nil {
		t.Fatalf("delete principal: %v", err)
	}
	eventually(t, 30*time.Second, func() error {
		var got v1alpha1.Principal
		err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: nsName, Name: principalName}, &got)
		if apierrors.IsNotFound(err) {
			return nil
		}
		if err != nil {
			return err
		}
		return &notReadyErr{msg: "principal still present with finalizers " + strings.Join(got.Finalizers, ",")}
	})
}

// TestPrincipalFinalizerPerNodePurgeConfirmation exercises the
// event-driven purge-confirmation branch in principal_finalizer.go's
// Reconcile Path B. The finalizer must:
//
//  1. Remain on the principal after Delete while
//     tenant DS DesiredNumberScheduled > 0 but no purge events
//     have arrived, AND
//  2. Be released once ONE `KerberosCcachePruned` event per desired
//     tenant pod is present (matching by Pod involvedObject.Name),
//     BEFORE the deletionTimeout fallback fires.
//
// This is the counterpart to TestPrincipalFinalizerLifecycle which
// exercises the DesiredNumberScheduled==0 "nothing to purge" branch.
// Together they cover both real-world paths a manage-mode principal
// can take out.
//
// Envtest has no kubelet, so we forge the DS status
// (DesiredNumberScheduled=2) by writing directly to the /status
// subresource. The `KerberosCcachePruned` events are synthesized
// with real timestamps AFTER Delete so they pass the finalizer's
// staleness filter (`ev.LastTimestamp.Before(kt.DeletionTimestamp)`
// rejects events from a prior UID incarnation).
func TestPrincipalFinalizerPerNodePurgeConfirmation(t *testing.T) {
	nsName := "fin-purge-ns"
	mustCreateNS(t, nsName, "2003/1")

	// Use a long deletion timeout so the timeout branch doesn't win
	// the race with our event synthesis. The whole test needs to
	// finish well inside this window.
	kcfName := "purge-kcf"
	if err := k8sClient.Create(testCtx, &v1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: kcfName, Namespace: nsName},
		Spec: v1alpha1.TenantSpec{
			Realm: "EXAMPLE.COM",
			Daemon: v1alpha1.DaemonTemplate{
				Image: "ghcr.io/dell/kerberator-daemon:v0.3.0",
			},
			DeletionTimeout: &metav1.Duration{Duration: 2 * time.Minute},
		},
	}); err != nil {
		t.Fatalf("create kcf: %v", err)
	}

	if err := k8sClient.Create(testCtx, &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: "purge-kt", Namespace: nsName},
		Data:       map[string][]byte{"svc.keytab": []byte("KT")},
	}); err != nil {
		t.Fatalf("create secret: %v", err)
	}

	const principalUID = int64(2003)
	principalName := "purge-principal"
	principal := &v1alpha1.Principal{
		ObjectMeta: metav1.ObjectMeta{Name: principalName, Namespace: nsName},
		Spec: v1alpha1.PrincipalSpec{
			UID:             principalUID,
			Principal:       "svc-2003@EXAMPLE.COM",
			Keytab:          "svc.keytab",
			TenantRef:       v1alpha1.TenantRef{Name: kcfName},
			KeytabSecretRef: &v1alpha1.SecretKeyRef{Name: "purge-kt", Key: "svc.keytab"},
		},
	}
	if err := k8sClient.Create(testCtx, principal); err != nil {
		t.Fatalf("create principal: %v", err)
	}

	// Wait for the tenant reconciler to create the DS.
	dsName := kcfName + "-kerberator-daemon"
	eventually(t, 30*time.Second, func() error {
		var ds appsv1.DaemonSet
		return k8sClient.Get(testCtx, types.NamespacedName{Namespace: nsName, Name: dsName}, &ds)
	})

	// Wait for the principal finalizer to be stamped.
	eventually(t, 20*time.Second, func() error {
		var got v1alpha1.Principal
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: nsName, Name: principalName}, &got); err != nil {
			return err
		}
		for _, f := range got.Finalizers {
			if f == controller.PrincipalFinalizer {
				return nil
			}
		}
		return &notReadyErr{msg: "finalizer not yet added"}
	})

	// Forge DS status: DesiredNumberScheduled=2. The TenantReconciler
	// re-writes the DS spec on every requeue but doesn't touch
	// .status. Writes to /status are separate from spec so the
	// reconciler's next spec-only upsert won't clobber this.
	var ds appsv1.DaemonSet
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: nsName, Name: dsName}, &ds); err != nil {
		t.Fatalf("get ds: %v", err)
	}
	ds.Status.DesiredNumberScheduled = 2
	if err := k8sClient.Status().Update(testCtx, &ds); err != nil {
		t.Fatalf("patch ds status: %v", err)
	}

	// Delete the principal. The finalizer must hold since no purge
	// events have been posted yet and DesiredNumberScheduled==2.
	if err := k8sClient.Delete(testCtx, principal); err != nil {
		t.Fatalf("delete principal: %v", err)
	}

	// Assert the finalizer is still holding (deletionTimestamp set,
	// finalizer still present). Poll for up to 5s so the observation
	// is stable, then assert.
	stableStart := time.Now()
	for time.Since(stableStart) < 3*time.Second {
		var got v1alpha1.Principal
		err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: nsName, Name: principalName}, &got)
		if apierrors.IsNotFound(err) {
			t.Fatalf("principal deleted before purge events were posted (finalizer released too early)")
		}
		if err != nil {
			t.Fatalf("get principal while waiting: %v", err)
		}
		if got.DeletionTimestamp.IsZero() {
			t.Fatalf("principal has no deletionTimestamp after Delete()")
		}
		found := false
		for _, f := range got.Finalizers {
			if f == controller.PrincipalFinalizer {
				found = true
				break
			}
		}
		if !found {
			t.Fatalf("finalizer released before any purge events posted")
		}
		time.Sleep(200 * time.Millisecond)
	}

	// Re-fetch the principal so we have its post-Delete DeletionTimestamp
	// to embed in the event timestamps. The finalizer's staleness
	// filter (principal_finalizer.go ~L329) requires
	// ev.LastTimestamp >= kt.DeletionTimestamp.
	var kt v1alpha1.Principal
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: nsName, Name: principalName}, &kt); err != nil {
		t.Fatalf("re-fetch principal: %v", err)
	}
	now := metav1.NewTime(time.Now().Add(time.Second)) // must be > DeletionTimestamp

	// Post two KerberosCcachePruned events, one per fake tenant
	// pod. The Pod involvedObjects need not exist as real Pods;
	// the finalizer only uses InvolvedObject.Name as a set key
	// (see principal_finalizer.go:335).
	for _, pod := range []string{"purge-kcf-kerberator-daemon-node-a", "purge-kcf-kerberator-daemon-node-b"} {
		ev := &corev1.Event{
			ObjectMeta: metav1.ObjectMeta{
				GenerateName: "purge-",
				Namespace:    nsName,
			},
			InvolvedObject: corev1.ObjectReference{
				Kind:      "Pod",
				Namespace: nsName,
				Name:      pod,
			},
			Reason:         controller.PurgeEventReason,
			Message:        fmt.Sprintf("Pruned stale ccache for uid=%d (no longer in roster)", principalUID),
			Type:           corev1.EventTypeNormal,
			FirstTimestamp: now,
			LastTimestamp:  now,
			Source:         corev1.EventSource{Component: "kerberator-daemon"},
		}
		if err := k8sClient.Create(testCtx, ev); err != nil {
			t.Fatalf("create purge event for %s: %v", pod, err)
		}
	}

	// Now the finalizer must release. The reconciler runs on principal
	// change; Events don't trigger it directly, but the RequeueAfter
	// path (~5s max) will pick this up.
	eventually(t, 20*time.Second, func() error {
		var got v1alpha1.Principal
		err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: nsName, Name: principalName}, &got)
		if apierrors.IsNotFound(err) {
			return nil
		}
		if err != nil {
			return err
		}
		return &notReadyErr{msg: "principal still present after per-node purge events; finalizers=" + strings.Join(got.Finalizers, ",")}
	})
}

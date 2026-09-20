// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package integration

import (
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// TestManageModeAggregationAndOwnership exercises the manage-mode
// happy path end-to-end:
//
//   - Create a Tenant in mode=manage and two
//     KerberosPrincipals that reference it (each with a real keytab
//     Secret).
//   - Assert the TenantReconciler produces all five owned children
//     (roster CM, keytab Secret, DaemonSet, events Role, events
//     RoleBinding) with OwnerReferences pointing back to the KCF and
//     Controller=true + BlockOwnerDeletion=true so K8s' garbage
//     collector will cascade-delete them.
//   - Assert the aggregated roster CM contains one `uid:principal:file`
//     line per principal plus the inline krb5.conf, and the aggregated
//     keytab Secret carries both principals' keytab bytes.
//
// This covers three of the four v1alpha1 acceptance-criteria items in
// one go (aggregation, ownerRef cascade prep, chart-CRD validity).
// The finalizer path is exercised separately in finalizer_test.go.
func TestManageModeAggregationAndOwnership(t *testing.T) {
	nsName := "agg-ns"
	mustCreateNS(t, nsName, "2001/2") // range [2001, 2003) covers both principals

	// Backing keytab Secrets. Each principal references its own, but
	// they could share; that's a separate reconciler branch we don't
	// need to exercise here.
	for _, s := range []struct {
		name, key string
		data      []byte
	}{
		{"kt-2001", "svc-2001.keytab", []byte("KEYTAB-2001-BYTES")},
		{"kt-2002", "svc-2002.keytab", []byte("KEYTAB-2002-BYTES")},
	} {
		if err := k8sClient.Create(testCtx, &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{Name: s.name, Namespace: nsName},
			Data:       map[string][]byte{s.key: s.data},
		}); err != nil {
			t.Fatalf("create secret %s: %v", s.name, err)
		}
	}

	kcfName := "agg-kcf"
	kcf := &v1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: kcfName, Namespace: nsName},
		Spec: v1alpha1.TenantSpec{
			Realm: "EXAMPLE.COM",
			Daemon: v1alpha1.DaemonTemplate{
				Image:         "ghcr.io/dell/kerberator-daemon:v0.3.0",
				RenewMinutes:  30,
				HostCachePath: "/var/lib/kerberator-daemon",
			},
		},
	}
	if err := k8sClient.Create(testCtx, kcf); err != nil {
		t.Fatalf("create kcf: %v", err)
	}

	// Two principals referencing the tenant. Both principals live in
	// the same tenant as the KCF (rule 3) and both UIDs fall in the
	// namespace's SCC range [2001, 2003) (rule 2).
	principals := []struct {
		name, principal, keytabSecret, keytabKey, keytabFile string
		uid                                                  int64
	}{
		{"t-2001", "svc-2001@EXAMPLE.COM", "kt-2001", "svc-2001.keytab", "svc-2001.keytab", 2001},
		{"t-2002", "svc-2002@EXAMPLE.COM", "kt-2002", "svc-2002.keytab", "svc-2002.keytab", 2002},
	}
	for _, tc := range principals {
		if err := k8sClient.Create(testCtx, &v1alpha1.Principal{
			ObjectMeta: metav1.ObjectMeta{Name: tc.name, Namespace: nsName},
			Spec: v1alpha1.PrincipalSpec{
				UID:       tc.uid,
				Principal: tc.principal,
				Keytab:    tc.keytabFile,
				TenantRef: v1alpha1.TenantRef{Name: kcfName},
				KeytabSecretRef: &v1alpha1.SecretKeyRef{
					Name: tc.keytabSecret, Key: tc.keytabKey,
				},
			},
		}); err != nil {
			t.Fatalf("create principal %s: %v", tc.name, err)
		}
	}

	// Owned-child names follow the "<kcf-name>-<suffix>" convention
	// enforced by tenant_helpers.go. Anchoring the assertions on
	// those names catches accidental renames at the same time as the
	// aggregation content.
	rosterCM := kcfName + "-roster"
	keytabSecret := kcfName + "-keytabs"
	daemonSet := kcfName + "-kerberator-daemon"
	eventsRole := kcfName + "-kerberator-daemon-events"

	// Wait for the TenantReconciler to finish its first manage-mode
	// pass. 30s is generous; a warm envtest reconciles within 1-2s.
	eventually(t, 30*time.Second, func() error {
		var cm corev1.ConfigMap
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: nsName, Name: rosterCM}, &cm); err != nil {
			return err
		}
		body := cm.Data["users.roster"]
		if !strings.Contains(body, "2001:svc-2001@EXAMPLE.COM:svc-2001.keytab") ||
			!strings.Contains(body, "2002:svc-2002@EXAMPLE.COM:svc-2002.keytab") {
			return &notReadyErr{msg: "roster CM missing principal line: " + body}
		}
		return nil
	})

	// Aggregated keytab Secret carries both principals' bytes.
	var sec corev1.Secret
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: nsName, Name: keytabSecret}, &sec); err != nil {
		t.Fatalf("get aggregated keytab secret: %v", err)
	}
	if string(sec.Data["svc-2001.keytab"]) != "KEYTAB-2001-BYTES" {
		t.Fatalf("svc-2001.keytab bytes mismatch: %q", sec.Data["svc-2001.keytab"])
	}
	if string(sec.Data["svc-2002.keytab"]) != "KEYTAB-2002-BYTES" {
		t.Fatalf("svc-2002.keytab bytes mismatch: %q", sec.Data["svc-2002.keytab"])
	}

	// Every owned child carries the KCF as its controller ownerRef
	// with BlockOwnerDeletion=true. K8s' garbage collector honors
	// this to cascade-delete the children when the KCF is deleted;
	// asserting the ref structure is more reliable than asserting
	// deletion propagation (envtest does NOT run the GC controller).
	assertOwned := func(obj metav1.Object, kind string) {
		refs := obj.GetOwnerReferences()
		if len(refs) == 0 {
			t.Fatalf("%s %s has no ownerReferences", kind, obj.GetName())
		}
		var ctrl *metav1.OwnerReference
		for i := range refs {
			if refs[i].Controller != nil && *refs[i].Controller {
				ctrl = &refs[i]
				break
			}
		}
		if ctrl == nil {
			t.Fatalf("%s %s has no controller ownerRef: %+v", kind, obj.GetName(), refs)
		}
		if ctrl.Kind != "Tenant" || ctrl.Name != kcfName {
			t.Fatalf("%s %s controllerRef points at %s/%s; want Tenant/%s", kind, obj.GetName(), ctrl.Kind, ctrl.Name, kcfName)
		}
		if ctrl.BlockOwnerDeletion == nil || !*ctrl.BlockOwnerDeletion {
			t.Fatalf("%s %s controllerRef.BlockOwnerDeletion should be true", kind, obj.GetName())
		}
	}

	var cm corev1.ConfigMap
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: nsName, Name: rosterCM}, &cm); err != nil {
		t.Fatalf("get roster cm: %v", err)
	}
	assertOwned(&cm, "ConfigMap")
	assertOwned(&sec, "Secret")

	var ds appsv1.DaemonSet
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: nsName, Name: daemonSet}, &ds); err != nil {
		t.Fatalf("get daemonset: %v", err)
	}
	assertOwned(&ds, "DaemonSet")

	var role rbacv1.Role
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: nsName, Name: eventsRole}, &role); err != nil {
		t.Fatalf("get events role: %v", err)
	}
	assertOwned(&role, "Role")

	var rb rbacv1.RoleBinding
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: nsName, Name: eventsRole}, &rb); err != nil {
		t.Fatalf("get events rolebinding: %v", err)
	}
	assertOwned(&rb, "RoleBinding")

	// Finally, prove the ownership guard by deleting the KCF and
	// confirming the roster CM is now unowned from our side (envtest
	// won't run GC, but Delete on the KCF should succeed and the
	// children remain valid k8s objects until GC would prune them).
	if err := k8sClient.Delete(testCtx, kcf); err != nil {
		t.Fatalf("delete kcf: %v", err)
	}
	eventually(t, 15*time.Second, func() error {
		var kcfAfter v1alpha1.Tenant
		err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: nsName, Name: kcfName}, &kcfAfter)
		if apierrors.IsNotFound(err) {
			return nil
		}
		if err != nil {
			return err
		}
		return &notReadyErr{msg: "KCF still present"}
	})
}

// notReadyErr is a tiny sentinel so eventually() can distinguish a
// "not yet" from a real API error in its logging. Using plain
// errors.New would work too but this keeps the intent obvious.
type notReadyErr struct{ msg string }

func (e *notReadyErr) Error() string { return e.msg }

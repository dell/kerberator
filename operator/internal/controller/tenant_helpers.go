// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package controller

import (
	"context"
	"fmt"
	"reflect"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/retry"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
	"github.com/dell/kerberator/operator/internal/tenant"
)

// retryOnConflict wraps k8s.io/client-go/util/retry with the default
// backoff. Used by every Update/StatusUpdate call so a concurrent
// write from another controller instance (leader failover, or an
// external actor patching status) doesn't wedge a reconcile with a
// stale ResourceVersion.
func retryOnConflict(fn func() error) error {
	return retry.RetryOnConflict(retry.DefaultRetry, fn)
}

// principalToTenantHandler enqueues the Tenant named
// by a Principal's spec.tenantRef.name. Principals with an
// empty Name (which admission rejects at the API boundary)
// enqueue nothing.
func principalToTenantHandler() handler.EventHandler {
	return handler.EnqueueRequestsFromMapFunc(func(_ context.Context, o client.Object) []reconcile.Request {
		kt, ok := o.(*v1alpha1.Principal)
		if !ok {
			return nil
		}
		if kt.Spec.TenantRef.Name == "" {
			return nil
		}
		return []reconcile.Request{{
			NamespacedName: types.NamespacedName{
				Namespace: kt.Namespace,
				Name:      kt.Spec.TenantRef.Name,
			},
		}}
	})
}

// controllerRefTo sets a controller-ownerRef on obj pointing at
// owner. Wraps controllerutil.SetControllerReference so the
// callers stay short.
func controllerRefTo(obj client.Object, owner metav1.Object, scheme *runtime.Scheme) error {
	if _, ok := owner.(runtime.Object); !ok {
		return fmt.Errorf("owner is not a runtime.Object: %T", owner)
	}
	return controllerutil.SetControllerReference(owner, obj, scheme)
}

// setCondition inserts or updates a Condition of the given Type
// in cs. LastTransitionTime is refreshed only when the Status
// actually changes; identical writes are no-ops.
func setCondition(cs *[]metav1.Condition, t string, s metav1.ConditionStatus, reason, message string) {
	now := metav1.Now()
	for i := range *cs {
		if (*cs)[i].Type == t {
			if (*cs)[i].Status != s {
				(*cs)[i].LastTransitionTime = now
			}
			(*cs)[i].Status = s
			(*cs)[i].Reason = reason
			(*cs)[i].Message = message
			return
		}
	}
	*cs = append(*cs, metav1.Condition{
		Type:               t,
		Status:             s,
		Reason:             reason,
		Message:            message,
		LastTransitionTime: now,
	})
}

// buildRosterCMData returns the Data map for the aggregated roster
// ConfigMap.
//
// Keys written:
//
//	tenant.RosterKey ("users.roster")       — always present, roster blob
//	                                            (may be empty if no
//	                                            included principals)
//	"krb5.conf"                              — present when the Tenant's
//	                                            spec.daemon.krb5Conf is
//	                                            non-empty
//	tenant.SelectorsKey ("selectors.json")    — present ONLY when at least
//	                                            one included Principal has a
//	                                            non-empty spec.nodeSelector.
//	                                            Absent otherwise so pre-
//	                                            v0.8 daemons and clusters
//	                                            without per-node targeting
//	                                            see the minimal CM shape
//	                                            they always did.
//	tenant.NodeLabelsKey ("node-labels.json") — present ONLY when
//	                                            selectorsJSON is also
//	                                            present. The daemon needs
//	                                            node labels only when
//	                                            selectors exist; skipping
//	                                            the write on the common
//	                                            "no selectors" path keeps
//	                                            the CM minimal.
func buildRosterCMData(rosterBlob, krb5Conf, selectorsJSON, nodeLabelsJSON string) map[string]string {
	data := map[string]string{tenant.RosterKey: rosterBlob}
	if krb5Conf != "" {
		data["krb5.conf"] = krb5Conf
	}
	if selectorsJSON != "" {
		data[tenant.SelectorsKey] = selectorsJSON
		// Only include node labels when we have selectors that
		// would need them — otherwise the daemon has no reason to
		// read this key.
		if nodeLabelsJSON != "" {
			data[tenant.NodeLabelsKey] = nodeLabelsJSON
		}
	}
	return data
}

// ownedBy reports whether obj has a controller ownerRef pointing
// at owner. Used as the safety guard on every write path: the
// reconciler refuses to modify an object that already exists but
// is not owned by the Tenant. Prevents a manage-mode
// reconcile from clobbering a pre-existing user-authored roster
// CM or DS that happened to share a name.
func ownedBy(obj metav1.Object, owner metav1.Object) bool {
	for _, ref := range obj.GetOwnerReferences() {
		if ref.Controller != nil && *ref.Controller &&
			ref.UID == owner.GetUID() &&
			ref.Name == owner.GetName() {
			return true
		}
	}
	return false
}

// upsertOwnedCM writes desired iff the existing CM (if any) is
// owned by kcf. If a CM with the same name exists without an
// ownerRef pointing at kcf, the reconcile fails with a clear
// error rather than clobbering it.
func (r *TenantReconciler) upsertOwnedCM(ctx context.Context, desired *corev1.ConfigMap, kcf *v1alpha1.Tenant) error {
	return retryOnConflict(func() error {
		var cur corev1.ConfigMap
		err := r.Get(ctx, types.NamespacedName{Namespace: desired.Namespace, Name: desired.Name}, &cur)
		if apierrors.IsNotFound(err) {
			return r.Create(ctx, desired)
		}
		if err != nil {
			return err
		}
		if !ownedBy(&cur, kcf) {
			return fmt.Errorf("refusing to modify ConfigMap %s/%s: not owned by Tenant %s",
				cur.Namespace, cur.Name, kcf.Name)
		}
		if reflect.DeepEqual(cur.Data, desired.Data) &&
			reflect.DeepEqual(cur.Labels, desired.Labels) {
			return nil
		}
		cur.Data = desired.Data
		cur.Labels = desired.Labels
		return r.Update(ctx, &cur)
	})
}

func (r *TenantReconciler) upsertOwnedSecret(ctx context.Context, desired *corev1.Secret, kcf *v1alpha1.Tenant) error {
	return retryOnConflict(func() error {
		var cur corev1.Secret
		err := r.Get(ctx, types.NamespacedName{Namespace: desired.Namespace, Name: desired.Name}, &cur)
		if apierrors.IsNotFound(err) {
			return r.Create(ctx, desired)
		}
		if err != nil {
			return err
		}
		if !ownedBy(&cur, kcf) {
			return fmt.Errorf("refusing to modify Secret %s/%s: not owned by Tenant %s",
				cur.Namespace, cur.Name, kcf.Name)
		}
		if reflect.DeepEqual(cur.Data, desired.Data) &&
			reflect.DeepEqual(cur.Labels, desired.Labels) {
			return nil
		}
		cur.Data = desired.Data
		cur.Labels = desired.Labels
		cur.Type = desired.Type
		return r.Update(ctx, &cur)
	})
}

func (r *TenantReconciler) upsertOwnedDS(ctx context.Context, desired *appsv1.DaemonSet, kcf *v1alpha1.Tenant) error {
	return retryOnConflict(func() error {
		var cur appsv1.DaemonSet
		err := r.Get(ctx, types.NamespacedName{Namespace: desired.Namespace, Name: desired.Name}, &cur)
		if apierrors.IsNotFound(err) {
			return r.Create(ctx, desired)
		}
		if err != nil {
			return err
		}
		if !ownedBy(&cur, kcf) {
			return fmt.Errorf("refusing to modify DaemonSet %s/%s: not owned by Tenant %s",
				cur.Namespace, cur.Name, kcf.Name)
		}
		// Only overwrite fields we care about; preserve status,
		// resourceVersion, generation.
		cur.Spec = desired.Spec
		cur.Labels = desired.Labels
		return r.Update(ctx, &cur)
	})
}

// upsertEventsRBAC ensures a Role + RoleBinding exist in the
// tenant's namespace granting create/patch on Events to the
// "default" ServiceAccount (which the tenant DaemonSet pods run
// as).
// Both objects are owned by kcf so `kubectl delete
// tenant` cascades cleanly.
func (r *TenantReconciler) upsertEventsRBAC(ctx context.Context, kcf *v1alpha1.Tenant, name string, labels map[string]string) error {
	desiredRole := &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: kcf.Namespace,
			Labels:    labels,
		},
		Rules: []rbacv1.PolicyRule{{
			APIGroups: []string{""},
			Resources: []string{"events"},
			Verbs:     []string{"create", "patch"},
		}},
	}
	if err := controllerRefTo(desiredRole, kcf, r.Scheme); err != nil {
		return fmt.Errorf("owner-ref role: %w", err)
	}
	if err := r.upsertOwnedRole(ctx, desiredRole, kcf); err != nil {
		return err
	}

	desiredRB := &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: kcf.Namespace,
			Labels:    labels,
		},
		RoleRef: rbacv1.RoleRef{
			APIGroup: rbacv1.GroupName,
			Kind:     "Role",
			Name:     name,
		},
		Subjects: []rbacv1.Subject{{
			Kind:      "ServiceAccount",
			Name:      "default",
			Namespace: kcf.Namespace,
		}},
	}
	if err := controllerRefTo(desiredRB, kcf, r.Scheme); err != nil {
		return fmt.Errorf("owner-ref rolebinding: %w", err)
	}
	return r.upsertOwnedRoleBinding(ctx, desiredRB, kcf)
}

func (r *TenantReconciler) upsertOwnedRole(ctx context.Context, desired *rbacv1.Role, kcf *v1alpha1.Tenant) error {
	return retryOnConflict(func() error {
		var cur rbacv1.Role
		err := r.Get(ctx, types.NamespacedName{Namespace: desired.Namespace, Name: desired.Name}, &cur)
		if apierrors.IsNotFound(err) {
			return r.Create(ctx, desired)
		}
		if err != nil {
			return err
		}
		if !ownedBy(&cur, kcf) {
			return fmt.Errorf("refusing to modify Role %s/%s: not owned by Tenant %s",
				cur.Namespace, cur.Name, kcf.Name)
		}
		if reflect.DeepEqual(cur.Rules, desired.Rules) &&
			reflect.DeepEqual(cur.Labels, desired.Labels) {
			return nil
		}
		cur.Rules = desired.Rules
		cur.Labels = desired.Labels
		return r.Update(ctx, &cur)
	})
}

func (r *TenantReconciler) upsertOwnedRoleBinding(ctx context.Context, desired *rbacv1.RoleBinding, kcf *v1alpha1.Tenant) error {
	return retryOnConflict(func() error {
		var cur rbacv1.RoleBinding
		err := r.Get(ctx, types.NamespacedName{Namespace: desired.Namespace, Name: desired.Name}, &cur)
		if apierrors.IsNotFound(err) {
			return r.Create(ctx, desired)
		}
		if err != nil {
			return err
		}
		if !ownedBy(&cur, kcf) {
			return fmt.Errorf("refusing to modify RoleBinding %s/%s: not owned by Tenant %s",
				cur.Namespace, cur.Name, kcf.Name)
		}
		// RoleRef is immutable; only touch Subjects and Labels.
		if reflect.DeepEqual(cur.Subjects, desired.Subjects) &&
			reflect.DeepEqual(cur.Labels, desired.Labels) {
			return nil
		}
		cur.Subjects = desired.Subjects
		cur.Labels = desired.Labels
		return r.Update(ctx, &cur)
	})
}

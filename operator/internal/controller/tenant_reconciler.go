// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// TenantReconciler owns Tenant CRs. On each
// reconcile it:
//
//  1. Lists every Principal in the CR's namespace whose
//     spec.tenantRef.name equals the CR's name.
//  2. Resolves each principal's keytab bytes via KeytabSecretRef.
//  3. Aggregates roster + keytab bytes deterministically.
//  4. Writes an aggregated roster ConfigMap and keytab Secret,
//     both ownerRef'd to the Tenant.
//  5. Ensures a Role + RoleBinding for per-UID Event emission on
//     the tenant pod's default ServiceAccount.
//  6. Renders and writes a tenant DaemonSet, ownerRef'd to the
//     Tenant.
//  7. Publishes rollup status: PrincipalCount, RosterHash,
//     DesiredNodes/ReadyNodes/ReadySummary, plus Conditions
//     (RosterConsistent, DaemonSetReady, KeytabsResolved, Ready).
//
// A single reconcile is idempotent: identical inputs produce
// identical child objects. The reconciler always writes through
// upsertOwned*, which enforces the owner-ref safeguard (never
// mutate an object it does not own) and retries on conflict.

package controller

import (
	"context"
	"fmt"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
	"github.com/dell/kerberator/operator/internal/tenant"
)

// TenantReconciler reconciles Tenant CRs.
type TenantReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder record.EventRecorder

	// RequeueInterval is how often to re-reconcile without an
	// event, so DS status rollups stay fresh.
	RequeueInterval time.Duration

	// DaemonImage is the daemon image for Tenants that do not set
	// spec.daemon.image. Wired from the operator's --daemon-image
	// flag; empty falls back to tenant.DefaultDaemonImage().
	DaemonImage string
}

// SetupTenantWithManager wires the TenantReconciler into the
// manager and watches:
//
//   - Tenant: primary kind.
//   - Principal: any principal change re-enqueues its
//     tenantRef.name.
//   - ConfigMap / Secret / DaemonSet: owned children re-enqueue
//     their owner (standard OwnerReference handling).
func (r *TenantReconciler) SetupTenantWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		Named("tenant").
		For(&v1alpha1.Tenant{}).
		Owns(&corev1.ConfigMap{}).
		Owns(&corev1.Secret{}).
		Owns(&appsv1.DaemonSet{}).
		Watches(&v1alpha1.Principal{}, principalToTenantHandler()).
		// v0.8: watch Node label changes so per-principal
		// spec.nodeSelector eligibility refreshes without a manual
		// touch. The predicate discards non-label-change updates
		// (kubelet emits Node status heartbeats every ~10s that
		// would otherwise re-reconcile every Tenant on every heartbeat).
		Watches(&corev1.Node{},
			handler.EnqueueRequestsFromMapFunc(r.mapNodeToTenants),
			builder.WithPredicates(nodeLabelsChangedPredicate())).
		Complete(r)
}

// Reconcile implements the Tenant control loop.
func (r *TenantReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues("kcf", req.NamespacedName.String())

	var kcf v1alpha1.Tenant
	if err := r.Get(ctx, req.NamespacedName, &kcf); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, fmt.Errorf("get kcf: %w", err)
	}

	status := v1alpha1.TenantStatus{}
	principals, err := r.listPrincipalsFor(ctx, &kcf)
	if err != nil {
		logger.Error(err, "list principals")
	}
	// PrincipalCount is the TOTAL number of Principals referencing this
	// Tenant (including disabled + deletion). `principals` is filtered
	// to roster-eligible only. We want the total for status so a
	// tenant with 5 principals, 2 disabled, still reports "5" rather
	// than "3" — matches what `kubectl get principals` would show a
	// human counting them.
	totalPrincipals, terr := r.countPrincipalsFor(ctx, &kcf)
	if terr != nil {
		logger.Error(terr, "count principals (falling back to eligible-only count)")
		status.PrincipalCount = int32(len(principals))
	} else {
		status.PrincipalCount = int32(totalPrincipals)
	}

	result, mErr := r.reconcileTenant(ctx, &kcf, principals, &status)
	if mErr != nil {
		logger.Error(mErr, "reconcile tenant")
		// Fall through to write status; return the error so
		// controller-runtime backs off appropriately.
		if writeErr := r.writeStatus(ctx, &kcf, status); writeErr != nil {
			logger.Error(writeErr, "write status after error")
		}
		return result, mErr
	}

	if err := r.writeStatus(ctx, &kcf, status); err != nil {
		return ctrl.Result{}, fmt.Errorf("write kcf status: %w", err)
	}
	return ctrl.Result{RequeueAfter: r.RequeueInterval}, nil
}

// reconcileTenant is the write-owning path: aggregates principals,
// writes owned children (roster CM, keytab Secret, events RBAC,
// DaemonSet), and populates status conditions.
func (r *TenantReconciler) reconcileTenant(
	ctx context.Context,
	kcf *v1alpha1.Tenant,
	principals []v1alpha1.Principal,
	status *v1alpha1.TenantStatus,
) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	// 1. Resolve each principal's keytab.
	sources, keytabsResolved := r.resolveKeytabs(ctx, kcf.Namespace, principals)

	// 2. Aggregate.
	agg := tenant.Aggregate(principals, sources)
	status.RosterHash = agg.RosterHash

	// 3. Names + labels for owned children.
	rosterCMName := kcf.Name + "-roster"
	keytabSecretName := kcf.Name + "-keytabs"
	dsName := kcf.Name + "-kerberator-daemon"
	labels := map[string]string{
		"app.kubernetes.io/name":       "kerberator-daemon",
		"app.kubernetes.io/instance":   kcf.Name,
		"app.kubernetes.io/managed-by": "kerberator-operator",
	}

	// 4. Write roster ConfigMap.
	//
	// If any included principal has a spec.nodeSelector, we also
	// snapshot every Node's labels into the CM so daemons can
	// filter locally without needing K8s API access. Skip the
	// fetch entirely on the common no-selectors path to keep the
	// CM minimal and the reconcile cheap.
	var nodeLabelsJSON string
	if agg.SelectorsJSON != "" {
		labelsByNode, err := r.snapshotNodeLabels(ctx)
		if err != nil {
			logger.Error(err, "snapshot node labels; selector-restricted principals will fail closed on affected nodes")
		} else {
			nodeLabelsJSON = tenant.BuildNodeLabelsJSON(labelsByNode)
		}
	}
	desiredCM := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      rosterCMName,
			Namespace: kcf.Namespace,
			Labels:    labels,
		},
		Data: buildRosterCMData(agg.RosterBlob, kcf.Spec.Daemon.Krb5Conf, agg.SelectorsJSON, nodeLabelsJSON),
	}
	if err := controllerRefTo(desiredCM, kcf, r.Scheme); err != nil {
		return ctrl.Result{}, fmt.Errorf("owner-ref cm: %w", err)
	}
	if err := r.upsertOwnedCM(ctx, desiredCM, kcf); err != nil {
		return ctrl.Result{}, fmt.Errorf("upsert roster cm: %w", err)
	}

	// 5. Write keytab Secret.
	desiredSecret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      keytabSecretName,
			Namespace: kcf.Namespace,
			Labels:    labels,
		},
		Type: corev1.SecretTypeOpaque,
		Data: agg.KeytabData,
	}
	if err := controllerRefTo(desiredSecret, kcf, r.Scheme); err != nil {
		return ctrl.Result{}, fmt.Errorf("owner-ref secret: %w", err)
	}
	if err := r.upsertOwnedSecret(ctx, desiredSecret, kcf); err != nil {
		return ctrl.Result{}, fmt.Errorf("upsert keytab secret: %w", err)
	}

	// 6. Ensure Role + RoleBinding for per-UID Event emission on the
	// tenant pod's default ServiceAccount. Empty
	// Role/RoleBinding on both sides is a fail-open no-op --
	// the tenant logs a single line and disables event
	// emission gracefully.
	roleName := kcf.Name + "-kerberator-daemon-events"
	if err := r.upsertEventsRBAC(ctx, kcf, roleName, labels); err != nil {
		return ctrl.Result{}, fmt.Errorf("upsert events RBAC: %w", err)
	}

	// 7. Render + write DaemonSet.
	desiredDS := tenant.BuildDaemonSet(kcf, tenant.DaemonSetInputs{
		Name:            dsName,
		Namespace:       kcf.Namespace,
		Labels:          labels,
		RosterConfigMap: rosterCMName,
		KeytabSecret:    keytabSecretName,
		MountKrb5Conf:   kcf.Spec.Daemon.Krb5Conf != "",
		DefaultImage:    r.DaemonImage,
	})
	if err := controllerRefTo(desiredDS, kcf, r.Scheme); err != nil {
		return ctrl.Result{}, fmt.Errorf("owner-ref ds: %w", err)
	}
	if err := r.upsertOwnedDS(ctx, desiredDS, kcf); err != nil {
		return ctrl.Result{}, fmt.Errorf("upsert daemonset: %w", err)
	}

	// 7. Roll DS status into KCF status.
	health, err := FetchTenantHealth(ctx, r.Client, kcf.Namespace, dsName)
	if err != nil {
		logger.Error(err, "fetch owned tenant health")
	}
	if health.Found {
		status.DesiredNodes = health.DesiredNodes
		status.ReadyNodes = health.ReadyNodes
		status.ReadySummary = fmt.Sprintf("%d/%d", health.ReadyNodes, health.DesiredNodes)
	}

	// 8. Conditions.
	if len(agg.DuplicateUIDs) > 0 {
		setCondition(&status.Conditions, "RosterConsistent", metav1.ConditionFalse, "DuplicateUID",
			fmt.Sprintf("duplicate uids: %v", agg.DuplicateUIDs))
	} else {
		setCondition(&status.Conditions, "RosterConsistent", metav1.ConditionTrue, "AllUIDsUnique", "")
	}

	if !keytabsResolved {
		setCondition(&status.Conditions, "KeytabsResolved", metav1.ConditionFalse, "SomeKeytabsMissing",
			"one or more principal KeytabSecretRefs could not be resolved")
	} else {
		setCondition(&status.Conditions, "KeytabsResolved", metav1.ConditionTrue, "AllKeytabsResolved", "")
	}

	switch {
	case !health.Found:
		setCondition(&status.Conditions, "DaemonSetReady", metav1.ConditionUnknown, "DaemonSetNotObserved", "")
	case health.DesiredNodes > 0 && health.ReadyNodes == health.DesiredNodes:
		setCondition(&status.Conditions, "DaemonSetReady", metav1.ConditionTrue, "AllPodsReady", "")
	default:
		setCondition(&status.Conditions, "DaemonSetReady", metav1.ConditionFalse, "SomePodsNotReady",
			fmt.Sprintf("%d/%d ready", health.ReadyNodes, health.DesiredNodes))
	}

	setCondition(&status.Conditions, "Ready", metav1.ConditionTrue, "Reconciled", "")

	return ctrl.Result{RequeueAfter: r.RequeueInterval}, nil
}

// listPrincipalsFor returns every Principal in the KCF's namespace whose
// spec.tenantRef.name matches the KCF's name AND that is eligible to
// participate in the aggregated roster (i.e., not being deleted and
// not soft-disabled).
//
// Two additional filters beyond tenantRef match:
//
//  1. Principals under deletion are excluded so the aggregated CM stops
//     advertising their UID immediately. The principal reconciler
//     holds the finalizer until the on-node ccache has actually
//     been purged; the CR object stays visible in `kubectl get`
//     (with a deletionTimestamp) until the purge confirms.
//
//  2. Principals with spec.disabled=true are excluded. This is a
//     soft-revoke primitive — the CR + keytab Secret + audit trail
//     stay put, but the principal is removed from the roster so the
//     daemon's next poll observes the change and prunes the
//     on-node ccache (subject to Tenant.spec.daemon.pruneStale).
//     Set spec.disabled=false to restore.
//
// If the parent Tenant itself is disabled, we short-circuit here and
// return an empty slice — the DaemonSet keeps running (ownerRef
// graph is preserved) but its next poll sees the empty roster and
// prunes every ccache.
func (r *TenantReconciler) listPrincipalsFor(ctx context.Context, kcf *v1alpha1.Tenant) ([]v1alpha1.Principal, error) {
	if kcf.Spec.Disabled {
		return nil, nil
	}
	var all v1alpha1.PrincipalList
	if err := r.List(ctx, &all, client.InNamespace(kcf.Namespace)); err != nil {
		return nil, err
	}
	out := make([]v1alpha1.Principal, 0, len(all.Items))
	for i := range all.Items {
		if all.Items[i].Spec.TenantRef.Name != kcf.Name {
			continue
		}
		if !all.Items[i].DeletionTimestamp.IsZero() {
			continue
		}
		if all.Items[i].Spec.Disabled {
			continue
		}
		out = append(out, all.Items[i])
	}
	return out, nil
}

// mapNodeToTenants is the enqueue map for the Watches(&corev1.Node{})
// binding: when a Node's labels change (per the predicate below) we
// re-reconcile every Tenant because any Tenant may have Principals whose
// selectors care about that node. There's no per-Tenant label
// filter — cheap List, and the aggregation is idempotent so
// re-reconciling extra Tenants is harmless.
func (r *TenantReconciler) mapNodeToTenants(ctx context.Context, _ client.Object) []reconcile.Request {
	var tenants v1alpha1.TenantList
	if err := r.List(ctx, &tenants); err != nil {
		return nil
	}
	out := make([]reconcile.Request, 0, len(tenants.Items))
	for i := range tenants.Items {
		out = append(out, reconcile.Request{
			NamespacedName: types.NamespacedName{
				Namespace: tenants.Items[i].Namespace,
				Name:      tenants.Items[i].Name,
			},
		})
	}
	return out
}

// nodeLabelsChangedPredicate returns a predicate that fires only on
// Node LABEL transitions. Kubelet emits Node status heartbeats
// every ~10s; without this filter we'd re-reconcile every Tenant on
// every heartbeat, cluster-wide. The filter is deliberately narrow:
//   - Create: always fire (a new Node might match existing selectors)
//   - Delete: always fire (an eligible Node going away affects the
//     effective set)
//   - Update: only fire if labels changed by deep-equal
//   - Generic: ignore (nothing we watch generates these)
func nodeLabelsChangedPredicate() predicate.Predicate {
	return predicate.Funcs{
		CreateFunc:  func(_ event.CreateEvent) bool { return true },
		DeleteFunc:  func(_ event.DeleteEvent) bool { return true },
		GenericFunc: func(_ event.GenericEvent) bool { return false },
		UpdateFunc: func(e event.UpdateEvent) bool {
			oldN, okOld := e.ObjectOld.(*corev1.Node)
			newN, okNew := e.ObjectNew.(*corev1.Node)
			if !okOld || !okNew {
				return false
			}
			return !stringMapsEqual(oldN.Labels, newN.Labels)
		},
	}
}

// stringMapsEqual is a cheap deep-equal for label maps.
// reflect.DeepEqual works but pulls in reflect for a hot path;
// this loop is a couple of ops per label.
func stringMapsEqual(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for k, va := range a {
		if vb, ok := b[k]; !ok || vb != va {
			return false
		}
	}
	return true
}

// snapshotNodeLabels lists every Node in the cluster and returns a
// map of nodeName → labels. Called ONLY when at least one included
// principal has a spec.nodeSelector (i.e., when the daemon needs the
// data). On the common no-selectors path we skip this fetch to
// keep reconcile cheap and the roster CM minimal.
//
// A List across all Nodes is O(nodes) each reconcile, but Nodes
// are cached by controller-runtime's client so subsequent calls
// hit the informer cache — costs are dominated by JSON encoding,
// not by API roundtrips.
func (r *TenantReconciler) snapshotNodeLabels(ctx context.Context) (map[string]map[string]string, error) {
	var nodes corev1.NodeList
	if err := r.List(ctx, &nodes); err != nil {
		return nil, fmt.Errorf("list nodes: %w", err)
	}
	out := make(map[string]map[string]string, len(nodes.Items))
	for i := range nodes.Items {
		n := &nodes.Items[i]
		// Copy the label map so mutating it later doesn't touch the
		// informer cache. controller-runtime returns pointers into
		// its cache; mutating them is a subtle correctness hazard.
		labels := make(map[string]string, len(n.Labels))
		for k, v := range n.Labels {
			labels[k] = v
		}
		out[n.Name] = labels
	}
	return out, nil
}

// countPrincipalsFor returns the total number of Principals in the KCF's
// namespace pointing at this Tenant, without any filter for disabled
// or deletion. Used by status.PrincipalCount so a human counting
// `kubectl get principals` sees the same number.
func (r *TenantReconciler) countPrincipalsFor(ctx context.Context, kcf *v1alpha1.Tenant) (int, error) {
	var all v1alpha1.PrincipalList
	if err := r.List(ctx, &all, client.InNamespace(kcf.Namespace)); err != nil {
		return 0, err
	}
	n := 0
	for i := range all.Items {
		if all.Items[i].Spec.TenantRef.Name == kcf.Name {
			n++
		}
	}
	return n, nil
}

// resolveKeytabs reads each principal's KeytabSecretRef and returns
// the resolved KeytabSource entries plus a bool indicating whether
// every reference resolved successfully. Missing references
// produce log entries but do not fail the reconcile; the
// aggregator will surface them as per-principal KeytabSecretMissing
// verdicts.
func (r *TenantReconciler) resolveKeytabs(
	ctx context.Context,
	namespace string,
	principals []v1alpha1.Principal,
) ([]tenant.KeytabSource, bool) {
	logger := log.FromContext(ctx)
	sources := make([]tenant.KeytabSource, 0, len(principals))
	allResolved := true
	for _, t := range principals {
		if t.Spec.KeytabSecretRef == nil {
			allResolved = false
			logger.Info("principal has no KeytabSecretRef; skipping",
				"principal", t.Name)
			continue
		}
		var sec corev1.Secret
		err := r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: t.Spec.KeytabSecretRef.Name}, &sec)
		if err != nil {
			allResolved = false
			logger.Info("principal KeytabSecretRef.Name not found",
				"principal", t.Name, "secret", t.Spec.KeytabSecretRef.Name, "err", err.Error())
			continue
		}
		key := t.Spec.KeytabSecretRef.Key
		bytes, ok := sec.Data[key]
		if !ok {
			allResolved = false
			logger.Info("principal KeytabSecretRef.Key missing in Secret",
				"principal", t.Name, "secret", t.Spec.KeytabSecretRef.Name, "key", key)
			continue
		}
		filename := t.Spec.Keytab
		if filename == "" {
			filename = key
		}
		sources = append(sources, tenant.KeytabSource{
			PrincipalName: t.Name,
			Filename:      filename,
			Bytes:         bytes,
		})
	}
	return sources, allResolved
}

// writeStatus writes the status subresource with retry-on-conflict.
func (r *TenantReconciler) writeStatus(ctx context.Context, kcf *v1alpha1.Tenant, status v1alpha1.TenantStatus) error {
	return retryOnConflict(func() error {
		var latest v1alpha1.Tenant
		if err := r.Get(ctx, types.NamespacedName{Namespace: kcf.Namespace, Name: kcf.Name}, &latest); err != nil {
			return err
		}
		latest.Status = status
		return r.Status().Update(ctx, &latest)
	})
}

// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// PrincipalReconciler owns the finalizer lifecycle on Principal
// CRs. Independent from TenantReconciler (which aggregates
// principals into the tenant's data plane).
//
// Lifecycle:
//
//  1. On a principal with no deletionTimestamp: ensure the finalizer
//     string `kerberator.dell.com/ccache-purged` is present in
//     metadata.finalizers.
//
//  2. On a principal with a non-zero deletionTimestamp:
//
//     a. Enqueue a tenant reconcile so the aggregation drops
//     this principal's UID from the roster ConfigMap on the next
//     pass. (listPrincipalsFor filters out DeletionTimestamp'd
//     principals, so no extra principal-side plumbing is needed.)
//
//     b. Look for on-node purge confirmation. The tenant image
//     will emit a `KerberosCcachePruned` Event on its own Pod
//     every time it removes an on-node ccache. If no
//     confirmation arrives, the finalizer falls back to a timeout: once
//     Tenant.spec.deletionTimeout has elapsed
//     since the principal's deletionTimestamp, the finalizer is
//     removed and a `CcachePurgeTimeout` Warning event is
//     recorded on the principal.
//
//     c. Once every tenant pod on the DaemonSet's desiredNodes
//     has emitted a purge event for this principal's UID, remove
//     the finalizer. If DesiredNodes == 0 (no scheduled
//     tenant pods), remove immediately — there's nothing to
//     purge.
//
// The timeout path is intentionally chosen over an indefinite
// wait: a stuck delete is worse for operators than a small window
// where the CR is gone but the on-node ccache lingers for one
// pruneStale cycle. The timeout event gives operators a signal to
// investigate the tenant's health.

package controller

import (
	"context"
	"fmt"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
	"github.com/dell/kerberator/operator/internal/events"
	"github.com/dell/kerberator/shared/keytab"
)

// PrincipalFinalizer is the finalizer string this controller manages
// on manage-mode Principal CRs. Exported so admission
// webhooks and tests can reference it.
const PrincipalFinalizer = "kerberator.dell.com/ccache-purged"

// PurgeEventReason is the corev1 Event.Reason the tenant emits
// when its pruneStale sweep removes an on-node ccache. Watched
// here to drive per-node purge confirmation.
const PurgeEventReason = "KerberosCcachePruned"

// DefaultDeletionTimeout is used when
// Tenant.spec.deletionTimeout is unset or zero.
const DefaultDeletionTimeout = 60 * time.Second

// PrincipalReconciler manages the finalizer lifecycle on
// Principal CRs and, when an EventCache is wired in, also
// publishes real per-UID status.nodes[] entries on those same
// principals.
type PrincipalReconciler struct {
	client.Client
	Recorder record.EventRecorder

	// Now is injected so tests can drive the timeout path
	// deterministically. Production wires this to time.Now.
	Now func() time.Time

	// EventCache is the per-UID observation store shared with
	// events.EventReconciler. Optional: a nil cache disables
	// the per-node status enrichment (the reconciler still runs its finalizer duties).
	EventCache *events.Cache
}

// SetupPrincipalWithManager wires the reconciler into the manager.
// Only Principal events drive Reconcile; the tenant
// reconciler handles all downstream aggregation.
func (r *PrincipalReconciler) SetupPrincipalWithManager(mgr ctrl.Manager) error {
	if r.Now == nil {
		r.Now = time.Now
	}
	b := ctrl.NewControllerManagedBy(mgr).
		Named("principal-finalizer").
		For(&v1alpha1.Principal{})

	// When wired with an EventCache, also watch
	// per-UID tenant Events and enqueue the matching principal so
	// status.nodes[] refreshes without waiting for the next
	// generic requeue. Filter to known reasons at the predicate
	// layer; map each event to zero-or-one principal by parsing
	// uid=<N> and looking up manage-mode principals with a
	// matching spec.UID in the event's namespace.
	if r.EventCache != nil {
		b = b.Watches(&corev1.Event{},
			handler.EnqueueRequestsFromMapFunc(r.mapEventToPrincipal),
			builder.WithPredicates(eventKnownReasonPredicate()))
	}

	// Watch daemon Pod readiness transitions so principal status
	// refreshes promptly after a DaemonSet rollout, node drain, or
	// pod crashloop. Without this the principal reconciler only
	// re-reconciles on Principal CR changes and on Kerberos*
	// daemon-emitted Events, both of which lag behind Pod-level
	// state on a rollout. See v0.5.2 CHANGELOG for context.
	b = b.Watches(&corev1.Pod{},
		handler.EnqueueRequestsFromMapFunc(r.mapDaemonPodToPrincipals),
		builder.WithPredicates(daemonPodReadyTransitionPredicate()))

	// Watch referenced Keytab Secrets so kvno / enctypes in
	// status refresh the moment an operator patches a Secret
	// (e.g., via `kubectl krb rotate-keytab`). Without this
	// watch, kvno drift only becomes visible after the next
	// Principal CR update or generic requeue.
	b = b.Watches(&corev1.Secret{},
		handler.EnqueueRequestsFromMapFunc(r.mapSecretToPrincipals))

	return b.Complete(r)
}

// mapSecretToPrincipals translates one Secret event into the
// enqueue-list of Principals whose spec.keytabSecretRef.name matches
// this Secret in the same namespace. Cheap List; the
// principal-per-namespace count is small enough that scanning is not
// a hot path.
func (r *PrincipalReconciler) mapSecretToPrincipals(ctx context.Context, obj client.Object) []reconcile.Request {
	sec, ok := obj.(*corev1.Secret)
	if !ok {
		return nil
	}
	var list v1alpha1.PrincipalList
	if err := r.List(ctx, &list, client.InNamespace(sec.Namespace)); err != nil {
		return nil
	}
	var out []reconcile.Request
	for i := range list.Items {
		u := &list.Items[i]
		if u.Spec.KeytabSecretRef == nil {
			continue
		}
		if u.Spec.KeytabSecretRef.Name != sec.Name {
			continue
		}
		out = append(out, reconcile.Request{NamespacedName: types.NamespacedName{
			Namespace: u.Namespace, Name: u.Name,
		}})
	}
	return out
}

// mapDaemonPodToPrincipals translates one kerberator-daemon Pod event
// into the enqueue-list of Principals whose tenantRef points at the
// same Tenant the Pod belongs to (via the
// `app.kubernetes.io/instance=<tenant-name>` label). Same shape as
// mapEventToPrincipal, keyed on Tenant instead of UID.
func (r *PrincipalReconciler) mapDaemonPodToPrincipals(ctx context.Context, obj client.Object) []reconcile.Request {
	pod, ok := obj.(*corev1.Pod)
	if !ok {
		return nil
	}
	tenantName := pod.Labels[DaemonPodInstanceLabelKey]
	if tenantName == "" {
		return nil
	}
	var list v1alpha1.PrincipalList
	if err := r.List(ctx, &list, client.InNamespace(pod.Namespace)); err != nil {
		return nil
	}
	var out []reconcile.Request
	for i := range list.Items {
		kt := &list.Items[i]
		if kt.Spec.TenantRef.Name != tenantName {
			continue
		}
		out = append(out, reconcile.Request{NamespacedName: types.NamespacedName{
			Namespace: kt.Namespace, Name: kt.Name,
		}})
	}
	return out
}

// mapEventToPrincipal translates one tenant Event into the
// enqueue-list of KerberosPrincipals that carry the event's UID in
// the same namespace.
func (r *PrincipalReconciler) mapEventToPrincipal(ctx context.Context, obj client.Object) []reconcile.Request {
	ev, ok := obj.(*corev1.Event)
	if !ok {
		return nil
	}
	uid, ok := events.ParseUIDFromEvent(ev)
	if !ok {
		return nil
	}
	var list v1alpha1.PrincipalList
	if err := r.List(ctx, &list, client.InNamespace(ev.Namespace)); err != nil {
		return nil
	}
	var out []reconcile.Request
	for i := range list.Items {
		kt := &list.Items[i]
		if kt.Spec.TenantRef.Name == "" {
			continue
		}
		if kt.Spec.UID != uid {
			continue
		}
		out = append(out, reconcile.Request{NamespacedName: types.NamespacedName{
			Namespace: kt.Namespace, Name: kt.Name,
		}})
	}
	return out
}

// Reconcile implements the finalizer lifecycle. It's safe to call
// on principals without a tenant in the cluster (removes the
// finalizer with a Warning), and after the CR is gone (returns
// nil).
func (r *PrincipalReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues("principal", req.NamespacedName.String())

	var kt v1alpha1.Principal
	if err := r.Get(ctx, req.NamespacedName, &kt); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, fmt.Errorf("get principal: %w", err)
	}

	// spec.tenantRef.name is required by the admission webhook,
	// but we defensively no-op if it's ever missing (e.g. a
	// hand-crafted client bypassed admission).
	if kt.Spec.TenantRef.Name == "" {
		return ctrl.Result{}, nil
	}

	// Path A: not being deleted -- ensure finalizer present and
	// publish per-UID status.
	if kt.DeletionTimestamp.IsZero() {
		if !hasFinalizer(&kt, PrincipalFinalizer) {
			if err := r.addFinalizer(ctx, &kt); err != nil {
				return ctrl.Result{}, fmt.Errorf("add finalizer: %w", err)
			}
		}
		// Status publication is best-effort: any error is
		// logged and swallowed. A failed status write does not
		// justify blocking the whole reconcile since it doesn't
		// affect correctness (the next reconcile will retry).
		if err := r.publishStatus(ctx, &kt); err != nil {
			logger.Info("publish principal status (per-UID)", "err", err.Error())
		}
		// Keytab-Secret-derived kvno/enctypes are a separate
		// publish path so a failure to read/parse the Secret
		// doesn't blow up the finalizer + per-UID node status
		// publish above. Both writers use retry-on-conflict
		// against the latest CR so ordering doesn't matter.
		if err := r.publishKvnoStatus(ctx, &kt); err != nil {
			logger.Info("publish principal status (kvno)", "err", err.Error())
		}
		return ctrl.Result{}, nil
	}

	// Path B: being deleted.
	if !hasFinalizer(&kt, PrincipalFinalizer) {
		// Nothing to hold; let the API server finish the delete.
		return ctrl.Result{}, nil
	}

	// Look up the parent tenant (best-effort). If it's gone
	// there's nothing left to confirm, so remove the finalizer
	// immediately with a Warning.
	var kcf v1alpha1.Tenant
	kcfErr := r.Get(ctx, types.NamespacedName{Namespace: kt.Namespace, Name: kt.Spec.TenantRef.Name}, &kcf)
	if apierrors.IsNotFound(kcfErr) {
		r.Recorder.Eventf(&kt, corev1.EventTypeWarning, "FactoryGone",
			"parent Tenant %q no longer exists; removing finalizer without confirming ccache purge",
			kt.Spec.TenantRef.Name)
		return ctrl.Result{}, r.removeFinalizer(ctx, &kt)
	}
	if kcfErr != nil {
		return ctrl.Result{}, fmt.Errorf("get parent tenant: %w", kcfErr)
	}

	// Try to confirm purge.
	confirmed, desiredNodes, err := r.purgeConfirmed(ctx, &kt, &kcf)
	if err != nil {
		logger.Error(err, "purge confirmation check failed; will retry")
	}
	if confirmed {
		logger.Info("ccache purge confirmed on all tenant nodes; removing finalizer",
			"desiredNodes", desiredNodes)
		return ctrl.Result{}, r.removeFinalizer(ctx, &kt)
	}

	// Not confirmed yet. Check timeout.
	timeout := DefaultDeletionTimeout
	if kcf.Spec.DeletionTimeout != nil && kcf.Spec.DeletionTimeout.Duration > 0 {
		timeout = kcf.Spec.DeletionTimeout.Duration
	}
	elapsed := r.Now().Sub(kt.DeletionTimestamp.Time)
	if elapsed >= timeout {
		r.Recorder.Eventf(&kt, corev1.EventTypeWarning, "CcachePurgeTimeout",
			"ccache purge not confirmed on all tenant nodes within %s; removing finalizer anyway", timeout)
		return ctrl.Result{}, r.removeFinalizer(ctx, &kt)
	}

	// Requeue for the remaining window. Cap at a reasonable
	// retry cadence so slow-clock skew doesn't stretch the wait.
	remaining := timeout - elapsed
	if remaining > 5*time.Second {
		remaining = 5 * time.Second
	}
	return ctrl.Result{RequeueAfter: remaining}, nil
}

// purgeConfirmed returns (true, desiredNodes, nil) iff every
// tenant pod on the parent DaemonSet has emitted a
// `KerberosCcachePruned` Event whose message references the
// principal's UID.
//
// Semantics chosen to be robust against transient conditions:
//
//   - DesiredNodes == 0: nothing to purge, return true.
//
//   - Tenant DaemonSet not found: return false with no error;
//     the reconciler will retry until timeout. This handles the
//     case where the DS is being (re)created concurrently with
//     the delete.
//
//   - Fewer distinct Pod involvedObjects with matching events
//     than DesiredNodes: return false; the reconciler will
//     requeue.
//
// principalHealth computes selector-aware Desired/Ready counts for a
// Principal. Both cases (unrestricted and selector-restricted) use
// the SAME per-node "Minted" count for readiness — never the raw
// DS-level pod-ready count. That distinction matters when the
// daemons are running fine but not currently minting this principal
// (e.g., the parent Tenant is disabled, or the Principal itself is
// disabled). Without this, the status would report Ready=True
// while every ccache was actually pruned. See the v0.8.3 fix.
//
// Desired is the principal's expected footprint:
//   - No selector: every worker where the DaemonSet is scheduled.
//   - Selector: only nodes whose labels match the selector.
//
// Ready is the count of Desired nodes whose per-node status
// reports Reason=Minted from the most recent sweep.
func (r *PrincipalReconciler) principalHealth(
	ctx context.Context,
	kt *v1alpha1.Principal,
	nodes []v1alpha1.NodeStatus,
	dsHealth TenantHealth,
) (desired int32, ready int32, err error) {
	// Determine the eligible node set.
	eligible := make(map[string]struct{})
	if len(kt.Spec.NodeSelector) == 0 {
		// Unrestricted: every node in the DS's per-node status
		// list is eligible.
		for _, ns := range nodes {
			eligible[ns.Name] = struct{}{}
		}
		desired = dsHealth.DesiredNodes
	} else {
		// Restricted: enumerate cluster Nodes and filter by
		// selector.
		var nodeList corev1.NodeList
		if err := r.List(ctx, &nodeList); err != nil {
			return 0, 0, fmt.Errorf("list nodes: %w", err)
		}
		for i := range nodeList.Items {
			n := &nodeList.Items[i]
			if nodeMatchesSelector(n.Labels, kt.Spec.NodeSelector) {
				eligible[n.Name] = struct{}{}
			}
		}
		desired = int32(len(eligible))
	}

	// Ready = eligible nodes with Reason=Minted. This is the ONLY
	// signal we trust for per-principal readiness; DS-level pod-ready
	// is not a proxy for "principal is minted here" when the daemon
	// might be up but skipping this principal.
	for _, ns := range nodes {
		if _, ok := eligible[ns.Name]; !ok {
			continue
		}
		if ns.Reason == "Minted" {
			ready++
		}
	}
	return desired, ready, nil
}

// nodeMatchesSelector is the same AND-match semantics the daemon
// uses (see pkg/watcher/nodefilter.go MatchesNode). Duplicated here
// because operator and daemon deliberately don't share code — but
// the logic is small and both sides have unit tests locking in
// identical behavior.
func nodeMatchesSelector(nodeLabels, selector map[string]string) bool {
	for k, v := range selector {
		if got, ok := nodeLabels[k]; !ok || got != v {
			return false
		}
	}
	return true
}

func (r *PrincipalReconciler) purgeConfirmed(
	ctx context.Context,
	kt *v1alpha1.Principal,
	kcf *v1alpha1.Tenant,
) (bool, int32, error) {
	dsName := kcf.Name + "-kerberator-daemon"

	var ds appsv1.DaemonSet
	err := r.Get(ctx, types.NamespacedName{Namespace: kcf.Namespace, Name: dsName}, &ds)
	if apierrors.IsNotFound(err) {
		// Tenant DS gone: nothing to confirm.
		return true, 0, nil
	}
	if err != nil {
		return false, 0, fmt.Errorf("get tenant ds: %w", err)
	}
	desired := ds.Status.DesiredNumberScheduled
	if desired == 0 {
		return true, 0, nil
	}

	// List Events in the principal's namespace matching the purge
	// reason. We filter by reason on the client side to keep the
	// fieldSelector portable across API server versions.
	var events corev1.EventList
	if err := r.List(ctx, &events, client.InNamespace(kt.Namespace)); err != nil {
		return false, desired, fmt.Errorf("list events: %w", err)
	}

	uidToken := fmt.Sprintf("uid=%d", kt.Spec.UID)
	confirmedPods := map[string]struct{}{}
	for _, ev := range events.Items {
		if ev.Reason != PurgeEventReason {
			continue
		}
		if ev.InvolvedObject.Kind != "Pod" {
			continue
		}
		if !strings.Contains(ev.Message, uidToken) {
			continue
		}
		// Only count events posted after the deletion started;
		// stale events from a previous incarnation of the same
		// UID would otherwise falsely confirm.
		if ev.LastTimestamp.IsZero() || ev.LastTimestamp.Before(kt.DeletionTimestamp) {
			// Also accept EventTime for the new events API.
			if ev.EventTime.IsZero() || ev.EventTime.Time.Before(kt.DeletionTimestamp.Time) {
				continue
			}
		}
		confirmedPods[ev.InvolvedObject.Name] = struct{}{}
	}

	return int32(len(confirmedPods)) >= desired, desired, nil
}

// addFinalizer appends PrincipalFinalizer to the principal's
// metadata.finalizers with retry-on-conflict. No-op if already
// present (checked at the call site).
func (r *PrincipalReconciler) addFinalizer(ctx context.Context, kt *v1alpha1.Principal) error {
	return retryOnConflict(func() error {
		var latest v1alpha1.Principal
		if err := r.Get(ctx, types.NamespacedName{Namespace: kt.Namespace, Name: kt.Name}, &latest); err != nil {
			return err
		}
		if hasFinalizer(&latest, PrincipalFinalizer) {
			return nil
		}
		latest.Finalizers = append(latest.Finalizers, PrincipalFinalizer)
		return r.Update(ctx, &latest)
	})
}

// removeFinalizer strips PrincipalFinalizer from the principal's
// metadata.finalizers with retry-on-conflict. No-op if already
// absent.
func (r *PrincipalReconciler) removeFinalizer(ctx context.Context, kt *v1alpha1.Principal) error {
	return retryOnConflict(func() error {
		var latest v1alpha1.Principal
		if err := r.Get(ctx, types.NamespacedName{Namespace: kt.Namespace, Name: kt.Name}, &latest); err != nil {
			if apierrors.IsNotFound(err) {
				return nil
			}
			return err
		}
		out := latest.Finalizers[:0]
		changed := false
		for _, f := range latest.Finalizers {
			if f == PrincipalFinalizer {
				changed = true
				continue
			}
			out = append(out, f)
		}
		if !changed {
			return nil
		}
		latest.Finalizers = out
		if err := r.Update(ctx, &latest); err != nil {
			return err
		}
		// Evict any cached per-UID observations for this
		// principal now that we've released its finalizer. Without
		// this, a stale event from the just-deleted UID could
		// leak into a replacement principal that reuses the same
		// UID minutes later (rare but possible where UIDs are
		// recycled aggressively).
		if r.EventCache != nil {
			r.EventCache.EvictUID(kt.Spec.UID)
		}
		return nil
	})
}

// publishStatus refreshes kt.Status.Nodes and kt.Status.Tenant
// for a live (non-deleting) manage-mode principal, using
// BuildNodeStatuses to mix per-UID Event observations with
// pod-readiness rollup. Only touched fields are:
//
//   - Status.Tenant               (Desired/Ready/ReadySummary)
//   - Status.Nodes                  (per-UID enriched)
//
// Conditions, RosterLine, and ObservedRosterResourceVersion are
// left untouched here so the manage-mode aggregation path in
// TenantReconciler stays the single writer of those fields.
//
// Best-effort: returns any error to the caller for logging but
// does not retry aggressively; the next reconcile will retry.
func (r *PrincipalReconciler) publishStatus(ctx context.Context, kt *v1alpha1.Principal) error {
	// Only run when the caller wired us up with a cache; the
	// legacy behavior stays intact when EventCache is nil.
	if r.EventCache == nil {
		return nil
	}
	// Manage-mode principals reference a Tenant by
	// name in the same namespace. Without that we can't derive
	// staleness or fetch tenant health.
	if kt.Spec.TenantRef.Name == "" {
		return nil
	}
	var kcf v1alpha1.Tenant
	if err := r.Get(ctx, types.NamespacedName{Namespace: kt.Namespace, Name: kt.Spec.TenantRef.Name}, &kcf); err != nil {
		if apierrors.IsNotFound(err) {
			return nil
		}
		return fmt.Errorf("get tenant for status: %w", err)
	}

	// The manage-mode DaemonSet's name is deterministic:
	// "<kcf.Name>-kerberator-daemon" (see tenant_reconciler.go).
	// No need to make that a spec field.
	dsName := kcf.Name + "-kerberator-daemon"
	health, err := FetchTenantHealth(ctx, r.Client, kcf.Namespace, dsName)
	if err != nil {
		return fmt.Errorf("fetch tenant health for status: %w", err)
	}
	if !health.Found {
		// No DS yet (tenant not fully reconciled). Nothing to
		// enrich; leave status alone.
		return nil
	}

	staleness := ResolveStaleness(&kcf)
	nodes := BuildNodeStatuses(kt, health, r.EventCache, r.Now(), staleness)

	// v0.8: compute effective health accounting for
	// spec.nodeSelector. If the principal restricts itself to a subset
	// of nodes, only THOSE nodes count toward Desired/Ready. This
	// avoids the "1/3" false-negative when 2 nodes are correctly
	// skipping the principal by design.
	//
	// principalHealth() takes the DS-level health as the ceiling
	// (can't have more eligible nodes than daemon pods) and
	// filters by selector using a live Node List.
	effectiveDesired, effectiveReady, healthErr := r.principalHealth(ctx, kt, nodes, health)
	if healthErr != nil {
		// Log-and-continue: if we can't compute selector-aware
		// health, fall back to the raw DS-level counts. Better a
		// slightly-wrong status than a stalled reconcile.
		log.FromContext(ctx).Error(healthErr, "compute selector-aware principal health; falling back to DS-level counts")
		effectiveDesired = health.DesiredNodes
		effectiveReady = health.ReadyNodes
	}

	// Write with retry-on-conflict.
	return retryOnConflict(func() error {
		var latest v1alpha1.Principal
		if err := r.Get(ctx, types.NamespacedName{Namespace: kt.Namespace, Name: kt.Name}, &latest); err != nil {
			return err
		}
		latest.Status.Nodes = nodes
		latest.Status.Tenant = v1alpha1.TenantHealth{
			DesiredNodes: effectiveDesired,
			ReadyNodes:   effectiveReady,
			ReadySummary: fmt.Sprintf("%d/%d", effectiveReady, effectiveDesired),
		}
		// Publish a top-level Ready condition so `kubectl krb list` and
		// generic `kubectl get`-style tooling can report a single
		// Ready-yes/no bit per Principal without walking nodes[]. The
		// derivation matches the intuitive definition: every desired
		// daemon pod is ready AND at least one daemon exists.
		// Detect Disabled ↔ Enabled transitions BEFORE we overwrite
		// the Ready condition. We emit a Kubernetes Event on
		// transition so audit trails / dashboards see the flip
		// distinctly from the steady-state condition writes (which
		// happen on every reconcile).
		wasDisabled := priorReadyReasonWas(latest.Status.Conditions, "Disabled")
		nowDisabled := latest.Spec.Disabled
		switch {
		case !wasDisabled && nowDisabled:
			reason := latest.Spec.DisabledReason
			if reason == "" {
				reason = "(no reason given)"
			}
			r.Recorder.Eventf(&latest, corev1.EventTypeNormal, "KerberosPrincipalDisabled",
				"principal disabled: %s", reason)
		case wasDisabled && !nowDisabled:
			r.Recorder.Eventf(&latest, corev1.EventTypeNormal, "KerberosPrincipalEnabled",
				"principal re-enabled; operator will re-add to Tenant roster on next reconcile")
		}

		switch {
		case latest.Spec.Disabled:
			// Disabled principals get a distinct condition so consumers
			// (kubectl-krb, dashboards, alerts) can tell "operator
			// intentionally quiesced this" apart from "something
			// broke." Reason is machine-readable; Message carries
			// the operator-supplied free-form label if any.
			msg := "principal is disabled"
			if r := latest.Spec.DisabledReason; r != "" {
				msg = fmt.Sprintf("principal is disabled (%s)", r)
			}
			setCondition(&latest.Status.Conditions, "Ready", metav1.ConditionUnknown,
				"Disabled", msg)
		case kcf.Spec.Disabled:
			// Parent Tenant disabled → cascade the state onto every
			// child Principal so consumers see the correct
			// intentional-quiesce reason rather than a generic
			// "SomeDaemonsNotReady". The daemons themselves are
			// running fine; they just aren't minting anything for
			// this tenant right now.
			setCondition(&latest.Status.Conditions, "Ready", metav1.ConditionUnknown,
				"TenantDisabled",
				fmt.Sprintf("parent Tenant %q is disabled; no ccaches minted anywhere", kcf.Name))
		case effectiveDesired == 0:
			// Two shapes reach here: no daemons at all, OR a
			// principal whose nodeSelector matches zero cluster
			// nodes. Distinguish by looking at the underlying DS
			// health so operators can tell "no daemons scheduled"
			// (transient) apart from "your selector is too tight"
			// (configuration issue).
			if health.DesiredNodes == 0 {
				setCondition(&latest.Status.Conditions, "Ready", metav1.ConditionUnknown,
					"NoDaemons", "no kerberator-daemon pods observed")
			} else {
				setCondition(&latest.Status.Conditions, "Ready", metav1.ConditionFalse,
					"NoEligibleNodes",
					"spec.nodeSelector matches zero cluster nodes; principal will not be minted anywhere")
			}
		case effectiveReady < effectiveDesired:
			setCondition(&latest.Status.Conditions, "Ready", metav1.ConditionFalse,
				"SomeDaemonsNotReady",
				fmt.Sprintf("%d/%d eligible daemon pods ready", effectiveReady, effectiveDesired))
		default:
			setCondition(&latest.Status.Conditions, "Ready", metav1.ConditionTrue,
				"AllDaemonsReady",
				fmt.Sprintf("%d/%d eligible daemon pods ready", effectiveReady, effectiveDesired))
		}
		return r.Status().Update(ctx, &latest)
	})
}

// hasFinalizer reports whether s appears in kt.Finalizers.
// priorReadyReasonWas reports whether the previously-recorded
// Ready condition on the Principal had the given Reason. Used to
// detect disabled↔enabled transitions so we emit exactly one
// Kubernetes Event per flip, not one per reconcile.
func priorReadyReasonWas(conds []metav1.Condition, reason string) bool {
	for _, c := range conds {
		if c.Type == "Ready" {
			return c.Reason == reason
		}
	}
	return false
}

func hasFinalizer(kt *v1alpha1.Principal, s string) bool {
	for _, f := range kt.Finalizers {
		if f == s {
			return true
		}
	}
	return false
}

// publishKvnoStatus reads the Principal's referenced Keytab Secret,
// parses the binary, and writes the derived kvno + enctypes onto
// Principal.status. Best-effort:
//
//   - If the Principal has no KeytabSecretRef (shouldn't happen — the
//     admission webhook requires it — but defensive), skip.
//   - If the Secret is missing or the key is empty, clear the
//     kvno fields in status (they'll re-populate the next
//     reconcile after the operator provisions the Secret).
//   - If parsing fails, log and leave status.KvnoFromSecret at 0.
//     Operators seeing 0 in `kubectl get principals` know to look at
//     the Secret bytes.
//
// The write is retry-on-conflict against the latest Principal CR so
// this can race safely with publishStatus above.
func (r *PrincipalReconciler) publishKvnoStatus(ctx context.Context, kt *v1alpha1.Principal) error {
	if kt.Spec.KeytabSecretRef == nil {
		return nil
	}
	ref := kt.Spec.KeytabSecretRef
	var sec corev1.Secret
	err := r.Get(ctx, types.NamespacedName{Namespace: kt.Namespace, Name: ref.Name}, &sec)
	if apierrors.IsNotFound(err) {
		// Secret not yet created: clear kvno so status reflects
		// reality rather than a stale generation.
		return r.writeKvnoStatus(ctx, kt, 0, "", nil)
	}
	if err != nil {
		return fmt.Errorf("get keytab secret %s/%s: %w", kt.Namespace, ref.Name, err)
	}
	data, ok := sec.Data[ref.Key]
	if !ok || len(data) == 0 {
		return r.writeKvnoStatus(ctx, kt, 0, "", nil)
	}
	kt5, err := keytab.Parse(data)
	if err != nil {
		// Unparseable bytes are worth logging but not blocking
		// on: the next Secret update will get another shot.
		log.FromContext(ctx).Info("parse keytab from secret",
			"secret", kt.Namespace+"/"+ref.Name, "key", ref.Key, "err", err.Error())
		return r.writeKvnoStatus(ctx, kt, 0, "", nil)
	}
	now := metav1.NewTime(r.Now())
	return r.writeKvnoStatus(ctx, kt, kt5.MaxKvno(), kt5.EnctypesString(), &now)
}

// writeKvnoStatus performs the retry-on-conflict status write.
// A no-op when the observed values already match the latest CR
// (avoids stamping KeytabObservedAt on unchanged data).
func (r *PrincipalReconciler) writeKvnoStatus(
	ctx context.Context,
	kt *v1alpha1.Principal,
	kvno uint32,
	enctypes string,
	observedAt *metav1.Time,
) error {
	return retryOnConflict(func() error {
		var latest v1alpha1.Principal
		if err := r.Get(ctx, types.NamespacedName{Namespace: kt.Namespace, Name: kt.Name}, &latest); err != nil {
			return err
		}
		// Skip no-op writes: same kvno and same enctypes mean
		// we already saw these bytes on a prior reconcile.
		// Preserving KeytabObservedAt from that prior reconcile
		// keeps dashboards honest ("last observed" = last time
		// we saw NEW bytes, not last time we reconciled).
		if latest.Status.KvnoFromSecret == kvno &&
			latest.Status.EnctypesFromSecret == enctypes {
			return nil
		}
		latest.Status.KvnoFromSecret = kvno
		latest.Status.EnctypesFromSecret = enctypes
		if observedAt != nil {
			latest.Status.KeytabObservedAt = observedAt
		} else {
			latest.Status.KeytabObservedAt = nil
		}
		return r.Status().Update(ctx, &latest)
	})
}

// silence unused import warning if the ctrl package ever removes
// aliases we rely on.
var _ = metav1.ObjectMeta{}

// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// PrincipalSpec describes one Kerberos identity managed by a
// tenant. Operators author the CR directly with TenantRef.Name
// pointing at a Tenant in the same namespace and
// KeytabSecretRef pointing at a Secret + key holding the principal's
// binary keytab. The Keytab field is derived by the controller
// from KeytabSecretRef.Key and typically left empty on input.
type PrincipalSpec struct {
	UID       int64     `json:"uid"`
	Principal string    `json:"principal"`
	Keytab    string    `json:"keytab,omitempty"`
	TenantRef TenantRef `json:"tenantRef"`

	// KeytabSecretRef is the source of the principal's binary keytab.
	// The controller reads the referenced Secret + key and
	// contributes the bytes to the tenant's aggregated keytab
	// Secret.
	KeytabSecretRef *SecretKeyRef `json:"keytabSecretRef,omitempty"`

	// Disabled, when true, excludes this Principal from the parent
	// Tenant's aggregated roster on the next reconcile. The
	// daemon's poll loop observes the roster hash change and
	// prunes the on-node ccache (subject to the Tenant's PruneStale
	// setting). Set to false to restore — the operator will
	// re-add the principal to the roster and the daemon will re-mint.
	//
	// This is a soft-revoke primitive: the Principal CR, keytab
	// Secret, per-node status history, and audit trail are all
	// preserved. Use `kubectl krb disable` / `enable` for the
	// day-two ergonomics.
	Disabled bool `json:"disabled,omitempty"`

	// DisabledReason is a free-form human label surfaced on
	// Principal.status.conditions[Ready].message. Not machine-parsed
	// by the operator or daemon, but conventions we recommend:
	//
	//   compromised   — credential suspected leaked; hard revoke intended
	//   maintenance   — routine operational disable
	//   audit-hold    — paused pending review
	//
	// Ignored when Disabled is false.
	DisabledReason string `json:"disabledReason,omitempty"`

	// NodeSelector, if non-empty, restricts this Principal's on-node
	// ccache to workers whose Kubernetes Node labels match ALL the
	// listed key=value pairs. Semantics mirror
	// DaemonSet.spec.template.spec.nodeSelector: it's an AND across
	// every key, and empty (default) means "match every node the
	// Tenant serves."
	//
	// Enforcement: the operator writes each principal's selector into
	// SelectorsKey of the aggregated roster ConfigMap. Every daemon
	// looks up its OWN node's labels (via the Kubernetes API on
	// startup and after each roster hash change) and skips roster
	// entries whose selector doesn't match. On a match transition
	// where the principal no longer applies, the daemon prunes the
	// stale on-node ccache subject to Tenant.spec.daemon.pruneStale.
	//
	// Use cases: geo-zone segregation, hardware-tier gating,
	// blast-radius reduction (place a principal's credential only on
	// nodes that actually host its workloads).
	NodeSelector map[string]string `json:"nodeSelector,omitempty"`
}

// TenantRef identifies which Tenant in the principal's
// namespace serves this principal. Name is required; the controller
// resolves tenant, roster/keytab aggregation targets, and DaemonSet
// ownership from the referenced CR.
type TenantRef struct {
	Name string `json:"name"`
}

// SecretKeyRef points at a specific data key within a Secret in
// the same namespace as the referring object.
type SecretKeyRef struct {
	Name string `json:"name"`
	Key  string `json:"key"`
}

// PrincipalStatus surfaces observed state for the principal.
type PrincipalStatus struct {
	ObservedRosterResourceVersion string             `json:"observedRosterResourceVersion,omitempty"`
	Tenant                        TenantHealth       `json:"tenant,omitempty"`
	Nodes                         []NodeStatus       `json:"nodes,omitempty"`
	Conditions                    []metav1.Condition `json:"conditions,omitempty"`

	// RosterLine is the exact `uid:principal:keytab` string the
	// controller produced for this principal on the last successful
	// aggregation. Debugging aid: mismatches with what the tenant
	// actually sees in its mounted roster ConfigMap point to a
	// missed reconcile or a manual edit of the aggregated CM.
	RosterLine string `json:"rosterLine,omitempty"`

	// KvnoFromSecret is the highest Kerberos KVNO (key version
	// number) observed in the referenced Keytab Secret on the
	// last successful reconcile. Populated by parsing the Secret
	// bytes; expected to match `kvno <principal>` against the
	// KDC and `klist -e -k` on the daemon's mounted keytab.
	//
	// Drift signal: if this diverges from the KDC's current
	// KVNO for the same principal (e.g., someone ran
	// `ipa-getkeytab` without rotating the Secret), fresh
	// kinit calls will fail with "Preauthentication failed".
	// `kubectl krb rotate-keytab <principal>` is the recovery.
	//
	// Zero means the Secret has not been observed yet or the
	// referenced key was empty/unparseable; the operator
	// re-attempts on the next reconcile.
	KvnoFromSecret uint32 `json:"kvnoFromSecret,omitempty"`

	// EnctypesFromSecret is the sorted, comma-separated set of
	// Kerberos encryption types present in the referenced Keytab
	// Secret. Written verbatim alongside KvnoFromSecret so
	// operators can eyeball drift between what FreeIPA / the
	// KDC issued and what the operator sees.
	//
	// Example: "aes128-cts-hmac-sha1-96, aes256-cts-hmac-sha1-96"
	EnctypesFromSecret string `json:"enctypesFromSecret,omitempty"`

	// KeytabObservedAt timestamps the reconcile that populated
	// KvnoFromSecret and EnctypesFromSecret. When Kerberator
	// rotates the Secret, this bumps to the reconcile that saw
	// the new bytes. Useful in dashboards to answer "when was
	// the last time we successfully re-read this principal's keytab
	// generation?"
	KeytabObservedAt *metav1.Time `json:"keytabObservedAt,omitempty"`
}

// TenantHealth is a rollup of the DaemonSet's per-node readiness.
type TenantHealth struct {
	DesiredNodes int32  `json:"desiredNodes"`
	ReadyNodes   int32  `json:"readyNodes"`
	ReadySummary string `json:"readySummary,omitempty"`
}

// NodeStatus reports the observed state of this principal on one
// tenant node. Two data sources feed into it:
//
//  1. Per-UID Events emitted by the tenant (see the
//     internal/events package). When a fresh event for
//     (podName, uid) is present, Reason carries the event's
//     Kerberos* reason (Minted, MintFailed, Pruned,
//     KeytabMissing), Message carries the event's human text,
//     and LastObserved is the event's timestamp. This is the
//     real per-principal-per-node signal.
//
//  2. Pod-readiness rollup as a fallback. If no fresh event
//     exists (bootstrap window, a daemon image that does not emit
//     Events, or events disabled cluster-wide),
//     Reason is derived from the tenant Pod's overall Ready
//     condition and Message is left empty.
//
// DaemonPodReady stays as the pod-level truth regardless of
// which path populated Reason; downstream tooling that only
// cares about "is the tenant alive on this node" can keep
// using it without knowing about events.
type NodeStatus struct {
	Name           string      `json:"name"`
	DaemonPodReady bool        `json:"daemonPodReady"`
	LastObserved   metav1.Time `json:"lastObserved,omitempty"`
	Reason         string      `json:"reason,omitempty"`

	// Message is the human-readable text from the most recent
	// per-UID tenant Event, when one is available. Empty when
	// the fallback pod-readiness path populated this NodeStatus.
	// Left empty (rather than a canned string) in the fallback
	// path so downstream UI code can distinguish "we don't know"
	// from "the tenant said this exactly".
	Message string `json:"message,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status

// Principal is the Schema for the principals API.
type Principal struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   PrincipalSpec   `json:"spec,omitempty"`
	Status PrincipalStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// PrincipalList contains a list of Principal.
type PrincipalList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Principal `json:"items"`
}

// DeepCopyInto and friends are hand-implemented below to avoid a
// code-gen step in this spike. If we grow the API we'll wire up
// controller-gen properly.

// --- DeepCopy scaffolding (hand-written) ---

func (in *TenantRef) DeepCopyInto(out *TenantRef) {
	*out = *in
}

func (in *SecretKeyRef) DeepCopyInto(out *SecretKeyRef) {
	*out = *in
}

func (in *TenantHealth) DeepCopyInto(out *TenantHealth) {
	*out = *in
}

func (in *NodeStatus) DeepCopyInto(out *NodeStatus) {
	*out = *in
	in.LastObserved.DeepCopyInto(&out.LastObserved)
}

func (in *PrincipalSpec) DeepCopyInto(out *PrincipalSpec) {
	*out = *in
	in.TenantRef.DeepCopyInto(&out.TenantRef)
	if in.KeytabSecretRef != nil {
		out.KeytabSecretRef = new(SecretKeyRef)
		in.KeytabSecretRef.DeepCopyInto(out.KeytabSecretRef)
	}
}

func (in *PrincipalStatus) DeepCopyInto(out *PrincipalStatus) {
	*out = *in
	in.Tenant.DeepCopyInto(&out.Tenant)
	if in.Nodes != nil {
		out.Nodes = make([]NodeStatus, len(in.Nodes))
		for i := range in.Nodes {
			in.Nodes[i].DeepCopyInto(&out.Nodes[i])
		}
	}
	if in.Conditions != nil {
		out.Conditions = make([]metav1.Condition, len(in.Conditions))
		for i := range in.Conditions {
			in.Conditions[i].DeepCopyInto(&out.Conditions[i])
		}
	}
	if in.KeytabObservedAt != nil {
		out.KeytabObservedAt = in.KeytabObservedAt.DeepCopy()
	}
}

func (in *Principal) DeepCopyInto(out *Principal) {
	*out = *in
	out.TypeMeta = in.TypeMeta
	in.ObjectMeta.DeepCopyInto(&out.ObjectMeta)
	in.Spec.DeepCopyInto(&out.Spec)
	in.Status.DeepCopyInto(&out.Status)
}

func (in *Principal) DeepCopy() *Principal {
	if in == nil {
		return nil
	}
	out := new(Principal)
	in.DeepCopyInto(out)
	return out
}

func (in *Principal) DeepCopyObject() runtime.Object {
	return in.DeepCopy()
}

func (in *PrincipalList) DeepCopyInto(out *PrincipalList) {
	*out = *in
	out.TypeMeta = in.TypeMeta
	in.ListMeta.DeepCopyInto(&out.ListMeta)
	if in.Items != nil {
		out.Items = make([]Principal, len(in.Items))
		for i := range in.Items {
			in.Items[i].DeepCopyInto(&out.Items[i])
		}
	}
}

func (in *PrincipalList) DeepCopy() *PrincipalList {
	if in == nil {
		return nil
	}
	out := new(PrincipalList)
	in.DeepCopyInto(out)
	return out
}

func (in *PrincipalList) DeepCopyObject() runtime.Object {
	return in.DeepCopy()
}

func init() {
	SchemeBuilder.Register(&Principal{}, &PrincipalList{})
}

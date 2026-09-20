// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package v1alpha1

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// TenantSpec is the desired state of one tenant
// installation. The controller owns the tenant DaemonSet, the
// aggregated roster ConfigMap, and the aggregated keytab Secret;
// all three are ownerRef'd to the Tenant so
// `kubectl delete tenant` cascades cleanly.
type TenantSpec struct {
	// Tenant this Tenant CR represents (e.g. EXAMPLE.COM). All
	// Principals referencing this Tenant must have principals in this
	// tenant; enforced by the admission webhook.
	Realm string `json:"realm"`

	// Daemon holds the values that get rendered into the per-node
	// kerberator-daemon DaemonSet the operator creates for this
	// Tenant.
	Daemon DaemonTemplate `json:"daemon,omitempty"`

	// DeletionTimeout is how long the principal finalizer will wait
	// for the tenant's per-UID `KerberosCcachePruned` Events
	// (one per desired tenant node) to confirm the on-node
	// ccache has been purged before removing the finalizer
	// anyway. Once elapsed, the reconciler removes the finalizer
	// and emits a CcachePurgeTimeout Warning event on the principal.
	// Zero (the default) resolves to 60s.
	DeletionTimeout *metav1.Duration `json:"deletionTimeout,omitempty"`

	// EventStalenessThreshold bounds how old a per-UID tenant
	// Event can be before the principal-status builder treats it as
	// missing and falls back to pod-readiness.
	//
	// The default (nil / zero) resolves to `max(5m, 2 *
	// tenant.renewMinutes minutes)` computed at consumption
	// time. That is: on a tenant renewing every 12h, an event
	// stays authoritative for a full day; on a tenant renewing
	// every minute (tests, pathological cases) the floor
	// keeps us from constantly declaring the tenant stale.
	EventStalenessThreshold *metav1.Duration `json:"eventStalenessThreshold,omitempty"`

	// Disabled, when true, tells the operator to aggregate an
	// EMPTY roster for this Tenant. Every child Principal is
	// effectively disabled at once; the DaemonSet keeps running
	// (so we don't lose its ownerRef graph), but on the next
	// poll it sees an empty roster and prunes every ccache. Set
	// back to false to restore.
	//
	// This is the "maintenance window / kill switch for the
	// whole KDC-side of this cluster" primitive. For per-principal
	// soft revocation see Principal.spec.disabled.
	Disabled bool `json:"disabled,omitempty"`
}

// DaemonTemplate is a controller-managed subset of the tenant
// Helm chart values. Only the fields we actually need to write are
// modeled here; everything else on the DaemonSet (labels, updates
// strategy, priorityClassName, ...) uses controller-hardcoded
// sensible defaults and can be surfaced as spec fields later if a
// real requirement shows up.
type DaemonTemplate struct {
	// Image overrides the daemon image. Empty uses the image the
	// operator was started with (--daemon-image, pinned by the chart
	// to the operator's own version).
	Image           string            `json:"image,omitempty"`
	ImagePullPolicy corev1.PullPolicy `json:"imagePullPolicy,omitempty"`
	// HostCachePath is the node directory the daemon writes
	// krb5cc_<uid> files into. Defaults to /tmp, which is where
	// rpc.gssd and other FILE-ccache consumers look.
	HostCachePath string                      `json:"hostCachePath,omitempty"`
	RenewMinutes  int32                       `json:"renewMinutes,omitempty"`
	PruneStale    bool                        `json:"pruneStale,omitempty"`
	HostAliases   []corev1.HostAlias          `json:"hostAliases,omitempty"`
	Resources     corev1.ResourceRequirements `json:"resources,omitempty"`
	NodeSelector  map[string]string           `json:"nodeSelector,omitempty"`
	Tolerations   []corev1.Toleration         `json:"tolerations,omitempty"`

	// Krb5Conf is the contents of the /etc/krb5.conf file to project
	// into every tenant pod. Required in practice: the stock tenant
	// image ships without a tenant-specific krb5.conf, so kinit inside
	// the pod has no way to reach the KDC. Rendered into the
	// aggregated roster ConfigMap under the key "krb5.conf" and
	// mounted at /etc/krb5.conf inside the container.
	//
	// Empty is permitted so tests and dev environments that provide
	// krb5.conf out-of-band (e.g. baked into a custom image) can
	// still omit it.
	Krb5Conf string `json:"krb5Conf,omitempty"`
}

// TenantStatus surfaces observed state.
type TenantStatus struct {
	// DesiredNodes / ReadyNodes / ReadySummary mirror the underlying
	// DaemonSet's rollup.
	DesiredNodes int32  `json:"desiredNodes,omitempty"`
	ReadyNodes   int32  `json:"readyNodes,omitempty"`
	ReadySummary string `json:"readySummary,omitempty"`

	// PrincipalCount is the number of Principal CRs whose
	// spec.tenantRef.name resolves to this tenant.
	PrincipalCount int32 `json:"principalCount,omitempty"`

	// RosterHash is a stable digest of the aggregated roster
	// contents. Consumers can use it for GitOps drift detection
	// or to short-circuit reconciles that would produce identical
	// output.
	RosterHash string `json:"rosterHash,omitempty"`

	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status

// Tenant is the Schema for the tenants API.
type Tenant struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   TenantSpec   `json:"spec,omitempty"`
	Status TenantStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// TenantList contains a list of Tenant.
type TenantList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Tenant `json:"items"`
}

// --- DeepCopy scaffolding (hand-written, matches the pattern from
// the original observer CRD). ---

func (in *DaemonTemplate) DeepCopyInto(out *DaemonTemplate) {
	*out = *in
	if in.HostAliases != nil {
		out.HostAliases = make([]corev1.HostAlias, len(in.HostAliases))
		for i := range in.HostAliases {
			in.HostAliases[i].DeepCopyInto(&out.HostAliases[i])
		}
	}
	in.Resources.DeepCopyInto(&out.Resources)
	if in.NodeSelector != nil {
		out.NodeSelector = make(map[string]string, len(in.NodeSelector))
		for k, v := range in.NodeSelector {
			out.NodeSelector[k] = v
		}
	}
	if in.Tolerations != nil {
		out.Tolerations = make([]corev1.Toleration, len(in.Tolerations))
		for i := range in.Tolerations {
			in.Tolerations[i].DeepCopyInto(&out.Tolerations[i])
		}
	}
}

func (in *TenantSpec) DeepCopyInto(out *TenantSpec) {
	*out = *in
	in.Daemon.DeepCopyInto(&out.Daemon)
	if in.DeletionTimeout != nil {
		out.DeletionTimeout = new(metav1.Duration)
		*out.DeletionTimeout = *in.DeletionTimeout
	}
	if in.EventStalenessThreshold != nil {
		out.EventStalenessThreshold = new(metav1.Duration)
		*out.EventStalenessThreshold = *in.EventStalenessThreshold
	}
}

func (in *TenantStatus) DeepCopyInto(out *TenantStatus) {
	*out = *in
	if in.Conditions != nil {
		out.Conditions = make([]metav1.Condition, len(in.Conditions))
		for i := range in.Conditions {
			in.Conditions[i].DeepCopyInto(&out.Conditions[i])
		}
	}
}

func (in *Tenant) DeepCopyInto(out *Tenant) {
	*out = *in
	out.TypeMeta = in.TypeMeta
	in.ObjectMeta.DeepCopyInto(&out.ObjectMeta)
	in.Spec.DeepCopyInto(&out.Spec)
	in.Status.DeepCopyInto(&out.Status)
}

func (in *Tenant) DeepCopy() *Tenant {
	if in == nil {
		return nil
	}
	out := new(Tenant)
	in.DeepCopyInto(out)
	return out
}

func (in *Tenant) DeepCopyObject() runtime.Object {
	return in.DeepCopy()
}

func (in *TenantList) DeepCopyInto(out *TenantList) {
	*out = *in
	out.TypeMeta = in.TypeMeta
	in.ListMeta.DeepCopyInto(&out.ListMeta)
	if in.Items != nil {
		out.Items = make([]Tenant, len(in.Items))
		for i := range in.Items {
			in.Items[i].DeepCopyInto(&out.Items[i])
		}
	}
}

func (in *TenantList) DeepCopy() *TenantList {
	if in == nil {
		return nil
	}
	out := new(TenantList)
	in.DeepCopyInto(out)
	return out
}

func (in *TenantList) DeepCopyObject() runtime.Object {
	return in.DeepCopy()
}

func init() {
	SchemeBuilder.Register(&Tenant{}, &TenantList{})
}

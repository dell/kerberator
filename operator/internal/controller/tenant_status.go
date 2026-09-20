// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package controller

import (
	"context"
	"fmt"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// TenantHealth is the reconciler's view of the tenant DaemonSet at
// one point in time. It's produced once per reconcile and shared
// across all Principal CRs that reference the same DS.
type TenantHealth struct {
	// Found is false if the DS doesn't exist in the namespace.
	Found bool

	DesiredNodes int32
	ReadyNodes   int32

	// Nodes is one entry per pod belonging to the DS. Nil if !Found.
	Nodes []v1alpha1.NodeStatus

	// PodByNode maps NodeStatus.Name (== pod.spec.nodeName)
	// back to the pod's own metadata.name. Populated alongside
	// Nodes. Used by BuildNodeStatuses to
	// translate a per-node status back to the (podName, uid)
	// key the events cache is indexed by.
	//
	// Kept as a separate field rather than a new field on
	// NodeStatus so the CRD schema (which serialises
	// NodeStatus) does not grow a pod-name field that leaks
	// implementation detail into the public API.
	PodByNode map[string]string
}

// FetchTenantHealth reads the DaemonSet and its pods and rolls them
// into a TenantHealth snapshot. Read-only: no writes to any object.
func FetchTenantHealth(ctx context.Context, c client.Client, namespace, dsName string) (TenantHealth, error) {
	var ds appsv1.DaemonSet
	err := c.Get(ctx, types.NamespacedName{Namespace: namespace, Name: dsName}, &ds)
	if apierrors.IsNotFound(err) {
		return TenantHealth{Found: false}, nil
	}
	if err != nil {
		return TenantHealth{}, fmt.Errorf("get daemonset %s/%s: %w", namespace, dsName, err)
	}

	h := TenantHealth{
		Found:        true,
		DesiredNodes: ds.Status.DesiredNumberScheduled,
		ReadyNodes:   ds.Status.NumberReady,
	}

	// Pods belonging to the DS. We rely on the DS's own selector rather
	// than a hard-coded label, so this works whether the DS is deployed
	// by the tenant's Helm chart, kustomize, or something else.
	sel, err := metav1.LabelSelectorAsSelector(ds.Spec.Selector)
	if err != nil {
		return h, fmt.Errorf("selector on daemonset %s/%s: %w", namespace, dsName, err)
	}

	var pods corev1.PodList
	if err := c.List(ctx, &pods, client.InNamespace(namespace), client.MatchingLabelsSelector{Selector: sel}); err != nil {
		return h, fmt.Errorf("list tenant pods: %w", err)
	}

	now := metav1.Now()
	h.PodByNode = make(map[string]string, len(pods.Items))
	for _, p := range pods.Items {
		// Ignore pods with no assigned node yet.
		if p.Spec.NodeName == "" {
			continue
		}
		ns := v1alpha1.NodeStatus{
			Name:         p.Spec.NodeName,
			LastObserved: now,
		}
		ns.DaemonPodReady, ns.Reason = podReady(&p)
		h.Nodes = append(h.Nodes, ns)
		h.PodByNode[p.Spec.NodeName] = p.Name
	}
	return h, nil
}

// podReady returns (true, "") if the pod has a Ready condition of
// True; otherwise (false, humanReason).
func podReady(p *corev1.Pod) (bool, string) {
	if p.Status.Phase != corev1.PodRunning {
		return false, fmt.Sprintf("Phase=%s", p.Status.Phase)
	}
	for _, c := range p.Status.Conditions {
		if c.Type == corev1.PodReady {
			if c.Status == corev1.ConditionTrue {
				return true, ""
			}
			if c.Reason != "" {
				return false, c.Reason
			}
			return false, "NotReady"
		}
	}
	return false, "NoReadyCondition"
}

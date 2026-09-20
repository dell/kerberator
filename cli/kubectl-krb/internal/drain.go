// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package internal

import (
	"context"
	"fmt"
	"io"

	"github.com/spf13/cobra"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// NewDrainNodeCmd builds `kubectl krb drain-node <node>` — cordons
// the target node so kubelet won't schedule new kerberator-daemon
// pods on it, and deletes the daemon Pod that's currently there.
//
// Unlike `kubectl drain`, which evicts every workload, this only
// touches the kerberator-daemon DaemonSet pods. Sandbox pods and
// other principals stay put. Use case: rolling the Kerberator daemon
// image on one worker for a canary, or quiescing kerberator writes
// during a KDC maintenance window.
//
// The DaemonSet controller re-creates the pod on uncordon, so this
// is a temporary quiesce, not a permanent removal.
func NewDrainNodeCmd(cf *ConfigFlags) *cobra.Command {
	var uncordon bool
	cmd := &cobra.Command{
		Use:   "drain-node <node>",
		Short: "Quiesce kerberator-daemon on one node (cordon + delete pod)",
		Args:  cobra.ExactArgs(1),
		Example: "  kubectl krb drain-node worker-1\n" +
			"  kubectl krb drain-node worker-1 --uncordon   # reverse (uncordon only)",
	}
	cmd.Flags().BoolVar(&uncordon, "uncordon", false, "Uncordon the node instead of draining.")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		return runDrain(cmd.Context(), cmd.OutOrStdout(), cf, args[0], uncordon)
	}
	return cmd
}

func runDrain(ctx context.Context, out io.Writer, cf *ConfigFlags, nodeName string, uncordon bool) error {
	cl, err := cf.Client()
	if err != nil {
		return err
	}

	var node corev1.Node
	if err := cl.Get(ctx, types.NamespacedName{Name: nodeName}, &node); err != nil {
		return fmt.Errorf("get node %s: %w", nodeName, err)
	}

	// Toggle .spec.unschedulable. Standard-verb-only mutation so this
	// works with any node-controlling admission policy the user has.
	before := node.Spec.Unschedulable
	node.Spec.Unschedulable = !uncordon
	if err := cl.Update(ctx, &node); err != nil {
		return fmt.Errorf("update node cordon: %w", err)
	}
	if before != node.Spec.Unschedulable {
		fmt.Fprintf(out, "node/%s cordoned=%v\n", nodeName, node.Spec.Unschedulable)
	}
	if uncordon {
		return nil
	}

	// Delete every kerberator-daemon pod on this node. Selector matches
	// pods labeled by the operator's DS builder.
	var pods corev1.PodList
	if err := cl.List(ctx, &pods,
		client.MatchingLabels{"app.kubernetes.io/name": "kerberator-daemon"},
		client.MatchingFields{"spec.nodeName": nodeName},
	); err != nil {
		// The MatchingFields selector requires an indexer that the
		// uncached client doesn't have. Fall back to filter in Go.
		if err := cl.List(ctx, &pods, client.MatchingLabels{"app.kubernetes.io/name": "kerberator-daemon"}); err != nil {
			return fmt.Errorf("list daemon pods: %w", err)
		}
	}
	for _, p := range pods.Items {
		if p.Spec.NodeName != nodeName {
			continue
		}
		if err := cl.Delete(ctx, &p); err != nil {
			return fmt.Errorf("delete pod %s/%s: %w", p.Namespace, p.Name, err)
		}
		fmt.Fprintf(out, "pod/%s deleted (namespace %s)\n", p.Name, p.Namespace)
	}
	return nil
}

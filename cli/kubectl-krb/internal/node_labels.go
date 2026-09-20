// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package internal

import (
	"context"
	"fmt"
	"io"
	"sort"
	"strings"

	"github.com/spf13/cobra"
	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// wellKnownSystemLabels is the set of Node label prefixes and exact
// keys that are set automatically by kubelet / cloud providers /
// K8s core, not by cluster operators. We hide these behind a
// --show-system flag / TUI toggle so first-time users looking for
// "what selectors can I use?" don't see 20 kubernetes.io/* labels
// they can't meaningfully key off of.
//
// Not exhaustive — anything not on this list is treated as
// operator-relevant. Add here if you find another auto-populated
// label class that clutters the picker.
var wellKnownSystemPrefixes = []string{
	"beta.kubernetes.io/",
	"node.kubernetes.io/",
	"node-role.kubernetes.io/",
	"topology.kubernetes.io/",
	"failure-domain.beta.kubernetes.io/",
	"csi-isilon.dellemc.com/", // Dell CSI driver auto-labels
	"csi.storage.k8s.io/",
	"kubernetes.io/arch",
	"kubernetes.io/os",
	"kubernetes.io/hostname", // kubelet-emitted; useful sometimes but shown behind --show-system
}

// NodeLabelSummary is the shape both the CLI subcommand and the TUI
// selector picker consume.
type NodeLabelSummary struct {
	// Key is the label key (e.g. "security-zone").
	Key string
	// Values are the distinct values that key takes across the
	// cluster's Nodes, sorted. Empty-value labels ("presence"
	// labels like `node-role.kubernetes.io/control-plane=`) get
	// [""] here.
	Values []string
	// Nodes is the count of Nodes that carry this key.
	Nodes int
	// System reports whether the key matched wellKnownSystemPrefixes.
	// The TUI + CLI both hide these unless the caller opts in.
	System bool
}

// ListNodeLabels fetches every Node in the cluster and aggregates
// their labels into a slice of NodeLabelSummary, sorted by key.
// Shared driver for both the TUI's selector picker and the CLI's
// `node-labels` subcommand.
func ListNodeLabels(ctx context.Context, cl client.Client) ([]NodeLabelSummary, error) {
	var nodes corev1.NodeList
	if err := cl.List(ctx, &nodes); err != nil {
		return nil, fmt.Errorf("list nodes: %w", err)
	}
	// keyValues[key] = set of distinct values
	// keyNodeCount[key] = number of nodes with that key
	keyValues := make(map[string]map[string]struct{})
	keyNodeCount := make(map[string]int)
	for i := range nodes.Items {
		for k, v := range nodes.Items[i].Labels {
			if _, ok := keyValues[k]; !ok {
				keyValues[k] = make(map[string]struct{})
			}
			keyValues[k][v] = struct{}{}
			keyNodeCount[k]++
		}
	}
	out := make([]NodeLabelSummary, 0, len(keyValues))
	for k, vs := range keyValues {
		values := make([]string, 0, len(vs))
		for v := range vs {
			values = append(values, v)
		}
		sort.Strings(values)
		out = append(out, NodeLabelSummary{
			Key:    k,
			Values: values,
			Nodes:  keyNodeCount[k],
			System: isSystemLabel(k),
		})
	}
	sort.Slice(out, func(i, j int) bool {
		// System labels last, then alphabetical.
		if out[i].System != out[j].System {
			return !out[i].System
		}
		return out[i].Key < out[j].Key
	})
	return out, nil
}

func isSystemLabel(key string) bool {
	for _, pfx := range wellKnownSystemPrefixes {
		if strings.HasSuffix(pfx, "/") {
			if strings.HasPrefix(key, pfx) {
				return true
			}
		} else if key == pfx {
			return true
		}
	}
	return false
}

// NewNodeLabelsCmd builds `kubectl krb node-labels`, which prints a
// grouped listing of Node labels available in the cluster. Used to
// discover what values a principal can plug into Principal.spec.nodeSelector.
//
// Two output shapes:
//
//	default: human-readable table (key = values ... N nodes)
//	--script: one k=v per line (grep-friendly)
func NewNodeLabelsCmd(cf *ConfigFlags) *cobra.Command {
	var showSystem, script bool
	cmd := &cobra.Command{
		Use:   "node-labels",
		Short: "List Node labels available in the cluster (for principal nodeSelectors)",
		Long: `List Kubernetes Node labels aggregated across the cluster, so operators
can discover what selectors are safe to attach to Principal.spec.nodeSelector.

Custom operator-set labels are shown first. Well-known system labels
(beta.kubernetes.io/*, node.kubernetes.io/*, csi-isilon.dellemc.com/*,
kubernetes.io/{arch,os,hostname}, etc.) are hidden unless
--show-system is passed.`,
		Example: `  # Default: operator-relevant labels only
  kubectl krb node-labels

  # Include kubelet + cloud-provider auto-set labels
  kubectl krb node-labels --show-system

  # Machine-readable, one k=v per line
  kubectl krb node-labels --script`,
	}
	cmd.Flags().BoolVar(&showSystem, "show-system", false, "Include system labels (kubernetes.io/*, etc.)")
	cmd.Flags().BoolVar(&script, "script", false, "One key=value per line, no headers or grouping")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		cl, err := cf.Client()
		if err != nil {
			return err
		}
		summaries, err := ListNodeLabels(cmd.Context(), cl)
		if err != nil {
			return err
		}
		return renderNodeLabels(cmd.OutOrStdout(), summaries, showSystem, script)
	}
	return cmd
}

func renderNodeLabels(out io.Writer, summaries []NodeLabelSummary, showSystem, script bool) error {
	if script {
		for _, s := range summaries {
			if s.System && !showSystem {
				continue
			}
			for _, v := range s.Values {
				fmt.Fprintf(out, "%s=%s\n", s.Key, v)
			}
		}
		return nil
	}
	// Human-readable grouped output. Two sections: custom labels,
	// then (if --show-system) system labels.
	var custom, system []NodeLabelSummary
	for _, s := range summaries {
		if s.System {
			system = append(system, s)
		} else {
			custom = append(custom, s)
		}
	}
	fmt.Fprintln(out, "CUSTOM LABELS (operator-set; use these in Principal.spec.nodeSelector)")
	if len(custom) == 0 {
		fmt.Fprintln(out, "  (none — no non-system labels found on any node. Try `kubectl label node ...`)")
	} else {
		for _, s := range custom {
			fmt.Fprintf(out, "  %-40s  %s   (%d node%s)\n",
				s.Key, formatValues(s.Values), s.Nodes, plural(s.Nodes))
		}
	}
	if showSystem && len(system) > 0 {
		fmt.Fprintln(out, "")
		fmt.Fprintln(out, "SYSTEM LABELS (kubelet / cloud provider / CSI driver auto-set)")
		for _, s := range system {
			fmt.Fprintf(out, "  %-40s  %s   (%d node%s)\n",
				s.Key, formatValues(s.Values), s.Nodes, plural(s.Nodes))
		}
	}
	return nil
}

func formatValues(vs []string) string {
	// Show up to 3 values; elide the rest.
	if len(vs) == 0 {
		return "(no values)"
	}
	if len(vs) <= 3 {
		out := make([]string, 0, len(vs))
		for _, v := range vs {
			if v == "" {
				out = append(out, `""`)
			} else {
				out = append(out, v)
			}
		}
		return strings.Join(out, ", ")
	}
	return fmt.Sprintf("%s, %s, %s, ... (+%d more)", vs[0], vs[1], vs[2], len(vs)-3)
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

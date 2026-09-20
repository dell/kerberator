// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package internal

import (
	"context"
	"fmt"
	"io"
	"sort"
	"strconv"

	"github.com/spf13/cobra"
	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	sharedevents "github.com/dell/kerberator/shared/events"
)

// NewEventsCmd builds `kubectl krb events [--uid <n>]` — a curated
// view of the four `Kerberos*` Events the daemon emits on its own
// Pods, optionally filtered by UID via the structured
// kerberator.dell.com/uid annotation.
//
// This is the "why did the mint fail on worker-3 for uid 2001"
// diagnostic: kubectl get events is too noisy across a full cluster,
// and grepping for `uid=<N>` in message text is brittle. The
// annotation-driven filter here matches how the operator itself
// correlates events to Principals.
func NewEventsCmd(cf *ConfigFlags) *cobra.Command {
	allNs := &AllNamespacesFlag{}
	var (
		uidFilter    int64
		reasonFilter string
	)
	cmd := &cobra.Command{
		Use:   "events",
		Short: "Show kerberator-daemon Events across the cluster",
		Example: "  kubectl krb events -A\n" +
			"  kubectl krb events --uid 2001\n" +
			"  kubectl krb events --reason KerberosMintFailed",
	}
	allNs.AddTo(cmd.Flags())
	cmd.Flags().Int64Var(&uidFilter, "uid", 0, "Only show Events for this principal UID (0 = no filter).")
	cmd.Flags().StringVar(&reasonFilter, "reason", "", "Only show Events with this Reason (e.g. KerberosMintFailed).")
	cmd.RunE = func(cmd *cobra.Command, _ []string) error {
		return runEvents(cmd.Context(), cmd.OutOrStdout(), cf, allNs.Value, uidFilter, reasonFilter)
	}
	return cmd
}

func runEvents(ctx context.Context, out io.Writer, cf *ConfigFlags, allNs bool, uidFilter int64, reasonFilter string) error {
	cl, err := cf.Client()
	if err != nil {
		return err
	}
	opts, err := listOpts(cf, allNs)
	if err != nil {
		return err
	}

	var evs corev1.EventList
	if err := cl.List(ctx, &evs, opts...); err != nil {
		return fmt.Errorf("list events: %w", err)
	}

	// Keep only the four daemon reasons — the annotation is our
	// primary filter but we also require a known Reason so we don't
	// accidentally surface an unrelated Event that happens to carry
	// the annotation (e.g. a hand-crafted `kubectl create event`).
	uidWant := ""
	if uidFilter != 0 {
		uidWant = strconv.FormatInt(uidFilter, 10)
	}
	filtered := evs.Items[:0]
	for _, e := range evs.Items {
		if _, known := sharedevents.KnownReasons[e.Reason]; !known {
			continue
		}
		if reasonFilter != "" && e.Reason != reasonFilter {
			continue
		}
		if uidWant != "" && e.Annotations[sharedevents.UIDAnnotationKey] != uidWant {
			continue
		}
		filtered = append(filtered, e)
	}
	sort.Slice(filtered, func(i, j int) bool {
		return filtered[i].LastTimestamp.After(filtered[j].LastTimestamp.Time)
	})

	if len(filtered) == 0 {
		fmt.Fprintln(out, "No kerberator daemon Events match.")
		return nil
	}
	fmt.Fprintln(out, "AGE\tNS\tPOD\tUID\tREASON\tMESSAGE")
	for _, e := range filtered {
		uid := e.Annotations[sharedevents.UIDAnnotationKey]
		if uid == "" {
			uid = "-"
		}
		fmt.Fprintf(out, "%s\t%s\t%s\t%s\t%s\t%s\n",
			ageStr(e.LastTimestamp.Time),
			e.Namespace,
			e.InvolvedObject.Name,
			uid,
			e.Reason,
			truncate(e.Message, 100),
		)
	}
	return nil
}

// Ensure sigs.k8s.io/controller-runtime client is exercised for
// linters that check unused deps. This file uses it via cl.List above.
var _ client.Client

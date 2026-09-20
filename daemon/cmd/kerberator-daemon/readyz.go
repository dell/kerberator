// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package main

import (
	"context"
	"fmt"
	"os"

	"github.com/dell/kerberator/daemon/pkg/watcher"
)

// runReadyz is the daemon binary's readiness check. Exits 0 if the
// daemon is READY on this node, non-zero otherwise. Ready means:
// every roster UID that WOULD apply on this node (post-selector-
// filter) has a corresponding /tmp/krb5cc_<uid> file present.
//
// This subcommand exists because pre-v0.8 the readiness probe was a
// simple shell one-liner that checked every roster UID
// unconditionally — which failed on nodes where the selector
// deliberately excludes a user. Rather than duplicate the filter
// logic in shell (needing jq to parse selectors.json), we reuse the
// Go filter and expose it via `kerberator-daemon readyz`.
//
// Args are identical to the main daemon in the paths+node-name it
// discovers, so the DaemonSet spec's readinessProbe can just invoke
// `/usr/local/bin/kerberator-daemon readyz` with the same env +
// mounts and get correct results.
func runReadyz() int {
	rosterPath := os.Getenv("READYZ_ROSTER")
	if rosterPath == "" {
		rosterPath = "/etc/kerberator-daemon/roster/users.roster"
	}
	cacheDir := os.Getenv("READYZ_CACHES")
	if cacheDir == "" {
		cacheDir = "/tmp"
	}
	selectorsPath := os.Getenv("READYZ_SELECTORS")
	if selectorsPath == "" {
		selectorsPath = "/etc/kerberator-daemon/roster/selectors.json"
	}
	nodeLabelsPath := os.Getenv("READYZ_NODE_LABELS")
	if nodeLabelsPath == "" {
		nodeLabelsPath = "/etc/kerberator-daemon/roster/node-labels.json"
	}
	nodeName := os.Getenv("NODE_NAME")

	f, err := os.Open(rosterPath)
	if err != nil {
		// No roster at all is a legitimate "not ready" — the CM
		// hasn't been projected yet.
		fmt.Fprintf(os.Stderr, "readyz: roster not available: %v\n", err)
		return 1
	}
	defer f.Close()
	reqs, _ := watcher.ParseRoster(f, "", cacheDir)
	// Apply the same filter the main loop uses so we agree on
	// what SHOULD be present on this node.
	p := &watcher.Processor{
		RosterPath:     rosterPath,
		CacheDir:       cacheDir,
		SelectorsPath:  selectorsPath,
		NodeLabelsPath: nodeLabelsPath,
		NodeName:       nodeName,
	}
	// Access the filter via a public wrapper on Processor.
	filtered := p.FilterEligibleForNode(context.Background(), reqs)
	if len(filtered) == 0 {
		// No expected UIDs on this node → trivially ready.
		return 0
	}
	// Every expected ccache must exist and be non-empty.
	for _, req := range filtered {
		info, err := os.Stat(req.CachePath)
		if err != nil {
			fmt.Fprintf(os.Stderr, "readyz: missing ccache for uid=%d at %s\n", req.UID, req.CachePath)
			return 1
		}
		if info.Size() == 0 {
			fmt.Fprintf(os.Stderr, "readyz: empty ccache for uid=%d at %s\n", req.UID, req.CachePath)
			return 1
		}
	}
	return 0
}

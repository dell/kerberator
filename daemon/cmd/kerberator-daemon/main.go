// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// Command kerberator-daemon is the per-node Kerberos ticket-cache
// minter. It reads a plaintext roster (uid:principal:keytab-filename
// per line), invokes `kinit -k` per line, and writes per-UID ccache
// files to a host-mounted directory. On a poll interval it re-hashes
// the roster and either (a) re-mints on change or (b) refreshes
// existing ccaches on renew cadence.
//
// Optional per-UID Kubernetes Events (Kerberos{Mint,Prune,Keytab}*)
// are emitted on the daemon's own Pod when POD_NAME/POD_NAMESPACE
// are set. The kerberator-operator ingests these to populate
// Principal.status.nodes[]. Event emission is fail-open: any API
// server error is logged and swallowed so mint operations never
// depend on Kubernetes reachability.
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"os"
	"os/signal"
	"strconv"
	"syscall"

	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"

	"github.com/dell/kerberator/daemon/internal/events"
	"github.com/dell/kerberator/daemon/pkg/watcher"
	"github.com/dell/kerberator/shared/version"
)

func main() {
	// Subcommand: `kerberator-daemon readyz` is invoked by the
	// DaemonSet's readinessProbe. Reuses the daemon's filter logic
	// (see cmd/kerberator-daemon/readyz.go) so pre-v0.8 shell probes
	// don't need to duplicate selector matching in awk/jq.
	if len(os.Args) > 1 && os.Args[1] == "readyz" {
		os.Exit(runReadyz())
	}
	if len(os.Args) > 1 && (os.Args[1] == "version" || os.Args[1] == "--version") {
		log.SetFlags(0)
		log.Printf("kerberator-daemon %s", version.String())
		return
	}

	log.Printf("==> kerberator-daemon %s starting <==", version.String())

	rosterPath := flag.String("roster", "/etc/roster/users.roster", "Path to the mapping file")
	keytabDir := flag.String("keytabs", "/keytabs", "Directory containing raw keytab binaries")
	cacheDir := flag.String("caches", "/host-tmp", "Target host directory to store minted ticket caches")
	renewFlag := flag.Int("renew-minutes", 0, "Renew interval in minutes (0 means use $RENEW_MINUTES or default 720)")
	pruneFlag := flag.Bool("prune-stale", false, "Delete ccaches for UIDs that were previously minted but have since been removed from the roster")

	// v0.8 flags for per-user nodeSelector filtering. Default to
	// files that live alongside the roster in the mounted CM. Absent
	// files = no filtering (pre-v0.8 behavior).
	selectorsPath := flag.String("selectors", "", "Path to selectors.json (defaults to <roster-dir>/selectors.json)")
	nodeLabelsPath := flag.String("node-labels", "", "Path to node-labels.json (defaults to <roster-dir>/node-labels.json)")
	flag.Parse()

	if *selectorsPath == "" {
		*selectorsPath = deriveAuxPath(*rosterPath, "selectors.json")
	}
	if *nodeLabelsPath == "" {
		*nodeLabelsPath = deriveAuxPath(*rosterPath, "node-labels.json")
	}

	renewMinutes := *renewFlag
	if renewMinutes == 0 {
		renewMinutes = 720
		if v := os.Getenv("RENEW_MINUTES"); v != "" {
			if parsed, err := strconv.Atoi(v); err == nil {
				renewMinutes = parsed
			}
		}
	}

	nodeName := os.Getenv("NODE_NAME")
	log.Printf("INFO: Configuration -> Roster: %s | Keytabs: %s | Host Output: %s | Interval: %dm | PruneStale: %v | NodeName: %q | Selectors: %s | NodeLabels: %s",
		*rosterPath, *keytabDir, *cacheDir, renewMinutes, *pruneFlag, nodeName, *selectorsPath, *nodeLabelsPath)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	proc := watcher.NewProcessor(*rosterPath, *keytabDir, *cacheDir)
	proc.PruneStale = *pruneFlag
	proc.NodeName = nodeName
	proc.SelectorsPath = *selectorsPath
	proc.NodeLabelsPath = *nodeLabelsPath

	// Per-UID event emission (best-effort). Downward-API env vars
	// POD_NAME + POD_NAMESPACE identify the Pod object the Events
	// hang off of. If either is missing, or if we can't build an
	// in-cluster client, we log once and continue with the no-op
	// recorder — the daemon's mint loop must not depend on
	// Kubernetes reachability.
	rec, stopRec := buildRecorder()
	defer stopRec()
	proc.Events = rec

	if err := proc.StartLoop(ctx, renewMinutes); err != nil && !errors.Is(err, context.Canceled) {
		log.Printf("INFO: loop exited: %v", err)
		stopRec()
		stop()
		os.Exit(1) //nolint:gocritic // deferred cleanups are invoked explicitly above
	}
}

// buildRecorder constructs the per-UID event recorder. Returns a
// NopRecorder + no-op stop function if event emission is disabled
// or unreachable; the daemon keeps running either way.
//
// Precedence:
//
//  1. If POD_NAME or POD_NAMESPACE is unset (dev laptop, unit
//     tests running the binary directly), use NopRecorder.
//  2. If rest.InClusterConfig fails, log the error and use
//     NopRecorder. The most common cause is running the binary
//     outside a Pod; the second most common is a broken
//     ServiceAccount projection, which the ops team wants to see
//     in stdout logs but shouldn't block ticket mints for.
//  3. Otherwise, build a KubeRecorder wired to the in-cluster
//     apiserver.
func buildRecorder() (events.Recorder, func()) {
	podName := os.Getenv("POD_NAME")
	podNs := os.Getenv("POD_NAMESPACE")
	if podName == "" || podNs == "" {
		log.Printf("INFO: POD_NAME/POD_NAMESPACE unset; per-UID event emission disabled")
		return events.NopRecorder{}, func() {}
	}
	cfg, err := rest.InClusterConfig()
	if err != nil {
		log.Printf("INFO: in-cluster config unavailable (%v); per-UID event emission disabled", err)
		return events.NopRecorder{}, func() {}
	}
	client, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		log.Printf("INFO: could not build kube client (%v); per-UID event emission disabled", err)
		return events.NopRecorder{}, func() {}
	}
	return events.NewKubeRecorder(client, podNs, podName, log.Default())
}

// deriveAuxPath produces the default path for an auxiliary CM key
// (selectors.json, node-labels.json) alongside the roster file.
// Example: /etc/kerberator-daemon/roster/users.roster + "selectors.json"
// → /etc/kerberator-daemon/roster/selectors.json.
func deriveAuxPath(rosterPath, filename string) string {
	// Use path.Dir semantics but with filepath to stay OS-agnostic
	// (though the daemon only runs on Linux).
	i := len(rosterPath) - 1
	for i >= 0 && rosterPath[i] != '/' {
		i--
	}
	if i < 0 {
		return filename
	}
	return rosterPath[:i+1] + filename
}

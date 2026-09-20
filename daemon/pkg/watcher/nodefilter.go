// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// Package watcher — nodefilter.go implements v0.8's per-user
// NodeSelector filtering.
//
// The operator writes two optional keys into the roster ConfigMap
// alongside `users.roster`:
//
//	selectors.json      {"<uid>": {"k": "v", ...}, ...}
//	node-labels.json    {"<nodeName>": {"k": "v", ...}, ...}
//
// On each sweep the daemon reads its OWN node's labels from
// node-labels.json (keyed by the NODE_NAME env var populated via
// the downward API) and skips roster rows whose selector doesn't
// match. Absent keys are treated as "no restrictions": every roster
// entry applies to this node, matching pre-v0.8 behavior.
//
// Design note: we don't need K8s API access. The operator has that,
// and everything the daemon needs to make a filtering decision
// arrives via the mounted ConfigMap. Keeps daemon RBAC minimal.
package watcher

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
)

// SelectorsMap is the parsed shape of selectors.json:
// uid → required label pairs (AND-composed).
type SelectorsMap map[int]map[string]string

// NodeLabelsMap is the parsed shape of node-labels.json:
// nodeName → labels.
type NodeLabelsMap map[string]map[string]string

// LoadSelectorsFile reads selectors.json from `path`. Missing file
// is not an error: an absent selectors.json means "no restrictions"
// and callers should treat every roster row as unrestricted. Only
// parse errors or file-read errors (other than not-found) surface
// as errors.
func LoadSelectorsFile(path string) (SelectorsMap, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	raw := map[string]map[string]string{}
	if err := json.Unmarshal(b, &raw); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	out := make(SelectorsMap, len(raw))
	for uidStr, labels := range raw {
		uid, err := strconv.Atoi(uidStr)
		if err != nil {
			// Skip malformed uid keys but keep going — a single
			// bad row shouldn't take out the whole filter.
			continue
		}
		if len(labels) == 0 {
			continue
		}
		out[uid] = labels
	}
	return out, nil
}

// LoadNodeLabelsFile reads node-labels.json from `path`. Same
// missing-file semantics as LoadSelectorsFile — absent means "the
// operator didn't project any node labels," which for selector-
// restricted users means "fail closed" (see MatchesNode below).
func LoadNodeLabelsFile(path string) (NodeLabelsMap, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	out := NodeLabelsMap{}
	if err := json.Unmarshal(b, &out); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	return out, nil
}

// FilterDecision is what MatchesNode returns for one (uid, node)
// pair. Applies=false means the daemon should skip this uid on
// this node — treat it as if it weren't in the roster.
type FilterDecision struct {
	Applies bool
	Reason  string
}

// MatchesNode decides whether the user at uid should be minted
// on the node named nodeName. Rules:
//
//  1. selectors is empty (no selectors.json OR uid absent from it):
//     the user is unrestricted → always Applies.
//  2. selectors has an empty map for uid: unrestricted → always
//     Applies. (Belt-and-braces; loadSelectorsFile skips these.)
//  3. selectors has a non-empty map for uid:
//     a. nodeName absent from nodeLabels: FAIL CLOSED. The daemon
//     can't prove the node's labels satisfy the selector, so
//     it declines to mint. Prevents accidental leakage during
//     transient operator/kubelet skew.
//     b. every k=v in selectors[uid] is present in nodeLabels[nodeName]:
//     Applies.
//     c. any k=v mismatch: skip.
func MatchesNode(uid int, nodeName string, selectors SelectorsMap, nodeLabels NodeLabelsMap) FilterDecision {
	req, restricted := selectors[uid]
	if !restricted || len(req) == 0 {
		return FilterDecision{Applies: true, Reason: "unrestricted"}
	}
	labels, hasLabels := nodeLabels[nodeName]
	if !hasLabels {
		return FilterDecision{
			Applies: false,
			Reason:  fmt.Sprintf("no node-labels entry for %q; failing closed", nodeName),
		}
	}
	for k, v := range req {
		if got, ok := labels[k]; !ok || got != v {
			return FilterDecision{
				Applies: false,
				Reason:  fmt.Sprintf("selector %s=%s does not match node label (got %q, present=%v)", k, v, got, ok),
			}
		}
	}
	return FilterDecision{Applies: true, Reason: "selector matched"}
}

// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// Package tenant implements the aggregation logic that a
// Tenant controller uses to project every
// Principal CR referencing it into two data-plane objects:
//
//   - an aggregated roster ConfigMap containing one
//     `uid:principal:keytab` line per principal, and
//   - an aggregated keytab Secret containing each principal's keytab
//     bytes at a stable key.
//
// The helpers in this file are pure: they consume slices of
// v1alpha1 objects and return the desired ConfigMap / Secret data
// plus a structured verdict describing per-principal admission
// (accepted, rejected due to duplicate UID, rejected due to
// missing keytab source). The reconciler in the controller
// package is the only caller and is responsible for turning
// verdicts into Conditions and Events.
package tenant

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// RosterKey is the canonical key inside the aggregated roster
// ConfigMap that holds the newline-delimited roster blob. It
// matches the default the tenant daemon expects.
const RosterKey = "users.roster"

// SelectorsKey is the ConfigMap key holding per-principal
// nodeSelectors as a JSON object: `{"<uid>": {"k": "v", ...}, ...}`.
// Only written when at least one Principal declares a non-empty
// spec.nodeSelector. Absent for backward compatibility with
// pre-v0.8 daemons, which know nothing about selectors and always
// process the full roster. New daemons see the absent key and
// behave identically to old ones.
const SelectorsKey = "selectors.json"

// NodeLabelsKey is the ConfigMap key holding a cluster-wide
// snapshot of node labels as a JSON object:
// `{"<nodeName>": {"k": "v", ...}, ...}`. Written only when the
// aggregated Tenant has at least one Principal with a non-empty
// nodeSelector (i.e., when the daemon would need it for
// filtering). Absent otherwise — daemons that don't see this key
// treat every roster row as "matches" (backward-compatible).
//
// The operator fills this in via a live List/Watch on Node
// objects; changes to Node labels drive Tenant reconciles via a
// mapping-based Watches() in the TenantReconciler setup.
const NodeLabelsKey = "node-labels.json"

// PrincipalVerdict describes what the aggregation loop decided to do
// with one principal. Exactly one of Rejected / Included is true.
type PrincipalVerdict struct {
	// PrincipalName is metadata.name of the Principal.
	PrincipalName string
	// UID is the principal's spec.uid. Retained even for rejected
	// principals so the reconciler can surface it in Conditions.
	UID int64
	// RosterLine is the exact `uid:principal:keytab` string that
	// went into the aggregated roster CM for this principal. Empty
	// for rejected principals.
	RosterLine string
	// Included is true if the principal contributed to the aggregated
	// roster + keytab Secret.
	Included bool
	// Rejected is true if the principal was skipped. Reason is a
	// short machine-readable token (DuplicateUID,
	// KeytabSecretMissing, KeytabKeyMissing, EmptyKeytabRef).
	// Message is a human-readable elaboration.
	Rejected bool
	Reason   string
	Message  string
}

// Aggregation is the full output of one aggregation pass.
type Aggregation struct {
	// RosterBlob is the value that should be written under
	// RosterKey in the aggregated ConfigMap. Always ends with a
	// trailing newline when non-empty so cat-style concatenation
	// with other tools stays sane.
	RosterBlob string

	// KeytabData is the map that should be written into the
	// aggregated keytab Secret's Data field. Keys are keytab
	// filenames (spec.keytab or, when unset, the principal's
	// KeytabSecretRef.Key). Values are the raw keytab bytes read
	// from each source Secret.
	//
	// If no principal was included, KeytabData contains a single
	// placeholder key ".placeholder" mapped to a zero-length
	// slice, so the DaemonSet's mount doesn't fail because of a
	// completely empty Secret.
	KeytabData map[string][]byte

	// SelectorsJSON is the value to write under SelectorsKey in
	// the aggregated ConfigMap. Empty when no included principal has
	// a spec.nodeSelector; the reconciler should NOT create the
	// key in that case (backward compat: pre-v0.8 daemons ignore
	// unknown keys anyway, but keeping the CM minimal keeps the
	// mental model simple). When non-empty, it's a canonicalized
	// JSON object: `{"<uid>": {"k": "v", ...}, ...}` sorted by
	// UID for stable diffs.
	SelectorsJSON string

	// RosterHash is a stable sha256 of RosterBlob, hex-encoded and
	// prefixed with "sha256:". Consumers can use it for drift
	// detection or to short-circuit reconciles that would produce
	// identical output.
	RosterHash string

	// Verdicts is one entry per input principal, in the input order,
	// documenting whether it was accepted or rejected and why.
	Verdicts []PrincipalVerdict

	// DuplicateUIDs lists every UID that appeared more than once
	// in the input. Empty on the happy path. When non-empty, none
	// of the colliding principals were included in RosterBlob (all
	// their verdicts are Rejected with reason=DuplicateUID) and
	// the reconciler should surface a RosterConsistent=False
	// condition on the Tenant.
	DuplicateUIDs []int64
}

// KeytabSource is the pair of (SecretName, Key, bytes) a principal
// contributes to the aggregated keytab Secret. The reconciler
// resolves KeytabSecretRef → bytes before calling Aggregate;
// keeping the resolution outside this package keeps it pure and
// easy to unit-test.
type KeytabSource struct {
	// PrincipalName identifies which Principal these bytes
	// belong to. Used to correlate with the principal slice passed
	// to Aggregate.
	PrincipalName string
	// Filename is the key under which the bytes should be
	// exposed in the aggregated Secret. Derived from spec.keytab
	// when non-empty, else from KeytabSecretRef.Key.
	Filename string
	// Bytes is the raw keytab content. May be nil if the source
	// Secret couldn't be resolved; in that case the reconciler
	// should NOT include a corresponding entry here at all, and
	// Aggregate will reject the principal with KeytabSecretMissing.
	Bytes []byte
}

// Aggregate consumes a slice of KerberosPrincipals and a slice of
// resolved keytab sources (typically produced by the reconciler
// after reading each principal's referenced Secret) and produces the
// desired aggregated state.
//
// Semantics:
//
//   - Duplicate UIDs: every principal sharing a colliding UID is
//     rejected. This is intentional over "last one wins" — a
//     silent-overwrite semantic makes it impossible for an
//     operator to notice that two teams have configured the same
//     principal, and the wrong keytab could quietly serve production
//     traffic.
//
//   - Missing keytab source: a principal whose entry is absent from
//     keytabSources is rejected with KeytabSecretMissing. The
//     reconciler should have surfaced the resolution failure
//     already (e.g., Secret not found), but the aggregation still
//     needs to know so it can leave the principal out of the roster.
//
//   - Empty inputs: an empty principal slice produces an empty
//     RosterBlob, a single-key placeholder KeytabData, and
//     RosterHash = sha256(""). The DaemonSet still runs happily
//     with nothing to do.
//
// Determinism: principals are sorted by UID for the roster blob so
// the output is a pure function of the input set. Two reconciles
// over the same set of CRs produce identical bytes, which lets
// the reconciler short-circuit on RosterHash equality.
func Aggregate(principals []v1alpha1.Principal, keytabSources []KeytabSource) Aggregation {
	// Index sources by principal name for O(1) lookup during the
	// main loop.
	sourceByPrincipal := make(map[string]KeytabSource, len(keytabSources))
	for _, ks := range keytabSources {
		sourceByPrincipal[ks.PrincipalName] = ks
	}

	// First pass: detect duplicate UIDs across the whole input,
	// independent of source resolution. A UID collision is a
	// harder failure than a missing keytab so it dominates the
	// verdict.
	uidCount := make(map[int64]int, len(principals))
	for _, t := range principals {
		uidCount[t.Spec.UID]++
	}
	dupSet := make(map[int64]struct{})
	for uid, n := range uidCount {
		if n > 1 {
			dupSet[uid] = struct{}{}
		}
	}

	verdicts := make([]PrincipalVerdict, 0, len(principals))
	included := make([]v1alpha1.Principal, 0, len(principals))
	includedSources := make([]KeytabSource, 0, len(principals))

	for _, t := range principals {
		v := PrincipalVerdict{PrincipalName: t.Name, UID: t.Spec.UID}

		if _, dup := dupSet[t.Spec.UID]; dup {
			v.Rejected = true
			v.Reason = "DuplicateUID"
			v.Message = fmt.Sprintf("uid %d is claimed by multiple Principal CRs", t.Spec.UID)
			verdicts = append(verdicts, v)
			continue
		}

		src, ok := sourceByPrincipal[t.Name]
		if !ok || src.Bytes == nil {
			v.Rejected = true
			v.Reason = "KeytabSecretMissing"
			v.Message = "referenced keytab Secret or key could not be resolved"
			verdicts = append(verdicts, v)
			continue
		}
		if src.Filename == "" {
			v.Rejected = true
			v.Reason = "EmptyKeytabFilename"
			v.Message = "keytab filename must be non-empty (set spec.keytab or spec.keytabSecretRef.key)"
			verdicts = append(verdicts, v)
			continue
		}

		v.Included = true
		v.RosterLine = fmt.Sprintf("%d:%s:%s", t.Spec.UID, t.Spec.Principal, src.Filename)
		verdicts = append(verdicts, v)
		included = append(included, t)
		includedSources = append(includedSources, src)
	}

	// Sort accepted entries by UID so the roster blob is a pure
	// function of the input SET (not the input ORDER). This makes
	// RosterHash stable across list-order flapping.
	type accepted struct {
		principal v1alpha1.Principal
		source    KeytabSource
		line      string
	}
	acc := make([]accepted, 0, len(included))
	for i, t := range included {
		acc = append(acc, accepted{
			principal: t,
			source:    includedSources[i],
			line:      fmt.Sprintf("%d:%s:%s", t.Spec.UID, t.Spec.Principal, includedSources[i].Filename),
		})
	}
	sort.SliceStable(acc, func(i, j int) bool { return acc[i].principal.Spec.UID < acc[j].principal.Spec.UID })

	var b strings.Builder
	keytabData := map[string][]byte{}
	for _, a := range acc {
		b.WriteString(a.line)
		b.WriteByte('\n')
		// Two principals pointing at the same filename with different
		// bytes is a subtle failure mode: the last one wins.
		// Reject rather than overwrite so it can't happen silently.
		if prev, ok := keytabData[a.source.Filename]; ok && !bytesEqual(prev, a.source.Bytes) {
			// Reverse the verdicts for the two colliding principals.
			markKeytabFilenameCollision(verdicts, a.source.Filename)
			// Also strip the roster line we just wrote.
			// This is rare enough that a cheap rebuild is fine.
			return rebuildAfterFilenameCollision(principals, keytabSources, a.source.Filename)
		}
		keytabData[a.source.Filename] = a.source.Bytes
	}

	if len(keytabData) == 0 {
		keytabData[".placeholder"] = []byte{}
	}

	roster := b.String()
	sum := sha256.Sum256([]byte(roster))
	hash := "sha256:" + hex.EncodeToString(sum[:])

	// Collect duplicate UIDs into a sorted slice for deterministic
	// Status output.
	dups := make([]int64, 0, len(dupSet))
	for uid := range dupSet {
		dups = append(dups, uid)
	}
	sort.Slice(dups, func(i, j int) bool { return dups[i] < dups[j] })

	// Build per-principal selectors object over the same UID-sorted
	// slice that fed the roster blob. Only INCLUDED principals
	// contribute; rejected ones (dup uid, missing keytab, etc.)
	// don't need a selector because they wouldn't be in the
	// roster anyway.
	includedPrincipals := make([]v1alpha1.Principal, 0, len(acc))
	for _, a := range acc {
		includedPrincipals = append(includedPrincipals, a.principal)
	}
	selectorsJSON := buildSelectorsJSON(includedPrincipals)

	return Aggregation{
		RosterBlob:    roster,
		KeytabData:    keytabData,
		RosterHash:    hash,
		Verdicts:      verdicts,
		DuplicateUIDs: dups,
		SelectorsJSON: selectorsJSON,
	}
}

// BuildNodeLabelsJSON produces the JSON string the operator writes
// to NodeLabelsKey. Input is a map of nodeName → labels; output is
// stable JSON with keys sorted (json.Marshal's map handling
// alphabetizes).
//
// Returns "" if the input is empty OR if any encoding error occurs;
// the reconciler treats "" as "don't write the key" so daemons see
// the CM they expect.
func BuildNodeLabelsJSON(nodeLabels map[string]map[string]string) string {
	if len(nodeLabels) == 0 {
		return ""
	}
	// Copy in only NON-EMPTY label maps. A node with zero labels
	// contributes no filtering value and only bloats the JSON.
	trimmed := make(map[string]map[string]string, len(nodeLabels))
	for name, labels := range nodeLabels {
		if len(labels) > 0 {
			trimmed[name] = labels
		}
	}
	if len(trimmed) == 0 {
		return ""
	}
	b, err := json.Marshal(trimmed)
	if err != nil {
		return ""
	}
	return string(b)
}

// buildSelectorsJSON produces a canonical JSON object of
// `{"<uid>": {"k":"v", ...}, ...}` for included principals whose
// spec.nodeSelector is non-empty. Returns the empty string when NO
// included principal has a selector — the reconciler treats that as
// "don't write the SelectorsKey to the CM" so daemons keep the old
// (fastest) code path.
//
// The input slice is expected to be UID-sorted (the caller in
// Aggregate uses the same ordering that built the roster blob) so
// the JSON output is stable across reconciles.
func buildSelectorsJSON(included []v1alpha1.Principal) string {
	obj := make(map[string]map[string]string)
	for _, t := range included {
		if len(t.Spec.NodeSelector) > 0 {
			obj[fmt.Sprintf("%d", t.Spec.UID)] = t.Spec.NodeSelector
		}
	}
	if len(obj) == 0 {
		return ""
	}
	// json.Marshal on a map[string]... produces alphabetically-
	// sorted keys, and since our keys are decimal uid strings,
	// alphabetic sort matches numeric sort for the cluster range
	// we care about (0-9999). Good enough for a stable canonical
	// form.
	b, err := json.Marshal(obj)
	if err != nil {
		// Should be impossible for map[string]map[string]string
		// but if it happens, return "" so the reconciler writes
		// no key and no daemon is confused by a partial JSON.
		return ""
	}
	return string(b)
}

// markKeytabFilenameCollision flips every verdict for principals
// contributing to a colliding filename into Rejected /
// KeytabFilenameCollision. Called from the collision recovery
// path.
func markKeytabFilenameCollision(verdicts []PrincipalVerdict, filename string) {
	for i := range verdicts {
		if !verdicts[i].Included {
			continue
		}
		// The roster line for this principal ended with :<filename>
		// so a suffix check is enough to catch the colliding
		// pair without threading filename data through the
		// verdict struct.
		if strings.HasSuffix(verdicts[i].RosterLine, ":"+filename) {
			verdicts[i].Included = false
			verdicts[i].Rejected = true
			verdicts[i].Reason = "KeytabFilenameCollision"
			verdicts[i].Message = fmt.Sprintf("keytab filename %q is claimed by multiple principals with different bytes", filename)
			verdicts[i].RosterLine = ""
		}
	}
}

// rebuildAfterFilenameCollision recursively re-runs aggregation
// with the offending filename's contributors excluded. In
// practice this collapses to a single retry because filename
// collisions are rare and the reconciler will refuse to accept
// them going forward.
func rebuildAfterFilenameCollision(
	principals []v1alpha1.Principal,
	keytabSources []KeytabSource,
	badFilename string,
) Aggregation {
	filteredSources := make([]KeytabSource, 0, len(keytabSources))
	for _, ks := range keytabSources {
		if ks.Filename == badFilename {
			continue
		}
		filteredSources = append(filteredSources, ks)
	}
	agg := Aggregate(principals, filteredSources)
	// Overlay the KeytabFilenameCollision reason so callers see
	// the real cause instead of KeytabSecretMissing.
	for i := range agg.Verdicts {
		if agg.Verdicts[i].Reason == "KeytabSecretMissing" {
			// See if this principal's original source was the
			// colliding filename.
			for _, ks := range keytabSources {
				if ks.PrincipalName == agg.Verdicts[i].PrincipalName && ks.Filename == badFilename {
					agg.Verdicts[i].Reason = "KeytabFilenameCollision"
					agg.Verdicts[i].Message = fmt.Sprintf("keytab filename %q is claimed by multiple principals with different bytes", badFilename)
					break
				}
			}
		}
	}
	return agg
}

func bytesEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package watcher

import (
	"bufio"
	"context"
	"crypto/md5"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/dell/kerberator/daemon/internal/events"
	"github.com/dell/kerberator/daemon/pkg/kerberos"
)

// DefaultPollInterval is how often the roster hash is checked for changes.
const DefaultPollInterval = 10 * time.Second

// ccacheNameRE matches the ticket cache filenames this tenant creates.
// It intentionally does not match arbitrary krb5cc_* files so a human
// kinit on the node stays untouched even if PruneStale is on.
var ccacheNameRE = regexp.MustCompile(`^krb5cc_(\d+)$`)

// Processor watches a roster file and mints Kerberos caches via a Minter.
//
// When PruneStale is true, ccaches for UIDs that were minted by *this*
// processor in a previous sweep but are no longer in the roster are
// deleted. Only UIDs that this processor has itself observed in a
// prior sweep are ever eligible for pruning: ccaches for foreign UIDs
// (e.g. a human `kinit` on the node) are never touched.
type Processor struct {
	RosterPath string
	KeytabDir  string
	CacheDir   string
	Minter     kerberos.Minter
	Logger     *log.Logger
	Poll       time.Duration
	PruneStale bool

	// NodeName is this daemon's Kubernetes Node name, populated by
	// the DaemonSet from the downward API (POD.spec.nodeName ->
	// env NODE_NAME). Empty when running outside Kubernetes
	// (tests, dev) — in that case per-user nodeSelectors are
	// treated as unrestricted (no filtering).
	NodeName string

	// SelectorsPath and NodeLabelsPath are optional companion
	// files to RosterPath, both written by the operator into the
	// same mounted ConfigMap. Missing files = no filtering
	// (matches pre-v0.8 behavior). See pkg/watcher/nodefilter.go.
	SelectorsPath  string
	NodeLabelsPath string

	// Remove is injected so tests do not need a real filesystem.
	// Defaults to os.Remove.
	Remove func(name string) error

	// Events posts per-UID Events on the tenant's Pod object so
	// operators (and the KerberosPrincipal reconciler in the sibling
	// operator project) can pinpoint which user is failing when
	// the pod's readiness probe goes red. Defaults to
	// events.NopRecorder{} -- best-effort semantics: any error
	// during Event emission is logged and swallowed so it never
	// affects mint correctness.
	Events events.Recorder

	// managed tracks UIDs this processor has minted in a previous
	// successful sweep. This is the eligibility set for pruning.
	managed map[int]struct{}
}

// NewProcessor returns a Processor with the default host Minter.
func NewProcessor(roster, keytab, cache string) *Processor {
	return &Processor{
		RosterPath: roster,
		KeytabDir:  keytab,
		CacheDir:   cache,
		Minter:     kerberos.NewHostMinter(),
		Logger:     log.Default(),
		Poll:       DefaultPollInterval,
		Remove:     os.Remove,
		Events:     events.NopRecorder{},
	}
}

// ParseRosterLine converts a single roster line into a TicketRequest.
// Returns (req, true, nil) on success; (_, false, nil) for skipped
// comments/empty lines; (_, false, err) on malformed lines.
func ParseRosterLine(line, keytabDir, cacheDir string) (kerberos.TicketRequest, bool, error) {
	trimmed := strings.TrimSpace(line)
	if trimmed == "" || strings.HasPrefix(trimmed, "#") {
		return kerberos.TicketRequest{}, false, nil
	}
	parts := strings.Split(trimmed, ":")
	if len(parts) != 3 {
		return kerberos.TicketRequest{}, false, fmt.Errorf("malformed roster line: %q", trimmed)
	}
	uidStr := strings.TrimSpace(parts[0])
	principal := strings.TrimSpace(parts[1])
	keytabName := strings.TrimSpace(parts[2])
	uid, err := strconv.Atoi(uidStr)
	if err != nil {
		return kerberos.TicketRequest{}, false, fmt.Errorf("invalid uid %q: %w", uidStr, err)
	}
	if principal == "" || keytabName == "" {
		return kerberos.TicketRequest{}, false, fmt.Errorf("empty principal or keytab in line: %q", trimmed)
	}
	req := kerberos.TicketRequest{
		UID:        uid,
		Principal:  principal,
		KeytabPath: filepath.Join(keytabDir, keytabName),
		CachePath:  filepath.Join(cacheDir, fmt.Sprintf("krb5cc_%d", uid)),
	}
	return req, true, nil
}

// ParseRoster parses all lines from r into TicketRequests, collecting errors
// for malformed lines rather than aborting the whole file.
func ParseRoster(r io.Reader, keytabDir, cacheDir string) ([]kerberos.TicketRequest, []error) {
	var reqs []kerberos.TicketRequest
	var errs []error
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		req, ok, err := ParseRosterLine(scanner.Text(), keytabDir, cacheDir)
		if err != nil {
			errs = append(errs, err)
			continue
		}
		if !ok {
			continue
		}
		reqs = append(reqs, req)
	}
	if err := scanner.Err(); err != nil {
		errs = append(errs, fmt.Errorf("scanner: %w", err))
	}
	return reqs, errs
}

// HashFile computes the MD5 of the file at path.
func HashFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := md5.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// hashInputs computes the MD5 of the roster + any optional
// companion files. This is the value the poll loop compares each
// tick, so a change to selectors.json or node-labels.json triggers
// a re-sweep just like a roster change does.
//
// Missing companion files are treated as "empty" contributions to
// the hash — matches the "no restrictions" fallback in
// applyNodeFilter. Roster missing IS an error, though: that
// invariant hasn't changed since v0.1.
func (p *Processor) hashInputs() (string, error) {
	h := md5.New()
	if err := hashFileInto(h, p.RosterPath, false); err != nil {
		return "", err
	}
	if p.SelectorsPath != "" {
		_ = hashFileInto(h, p.SelectorsPath, true)
	}
	if p.NodeLabelsPath != "" {
		_ = hashFileInto(h, p.NodeLabelsPath, true)
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// hashFileInto writes the file's bytes into h. When optional is
// true, missing/unreadable files contribute nothing (no error).
// A magic separator between files prevents "same combined blob,
// different split" collisions.
func hashFileInto(h io.Writer, path string, optional bool) error {
	f, err := os.Open(path)
	if err != nil {
		if optional && os.IsNotExist(err) {
			return nil
		}
		return err
	}
	defer f.Close()
	if _, err := io.Copy(h, f); err != nil {
		return err
	}
	// Sentinel between file contents. NUL is guaranteed absent
	// from the roster/JSON payloads we care about.
	_, _ = h.Write([]byte{0})
	return nil
}

// ProcessRoster reads the roster and mints caches for every valid entry.
// Returns counts of successes and failures.
func (p *Processor) ProcessRoster(ctx context.Context) (int, int) {
	f, err := os.Open(p.RosterPath)
	if err != nil {
		p.Logger.Printf("ERROR: Failed to open roster file: %v", err)
		return 0, 1
	}
	defer f.Close()

	reqs, parseErrs := ParseRoster(f, p.KeytabDir, p.CacheDir)
	ok, fail := 0, len(parseErrs)
	for _, e := range parseErrs {
		p.Logger.Printf("WARN: %v", e)
	}

	// v0.8 filter: per-user nodeSelector eligibility.
	// selectors.json and node-labels.json are optional CM keys the
	// operator writes ONLY when at least one Principal has a
	// spec.nodeSelector. When absent, ApplyNodeFilter is a no-op
	// and we mint everything (matches pre-v0.8 behavior).
	reqs = p.applyNodeFilter(reqs)

	// inRoster is every UID appearing in the FILTERED roster this sweep,
	// regardless of mint success. Pruning uses this set so a transient
	// KDC failure does not delete a still-configured user's ccache.
	// UIDs filtered out by nodeSelector are excluded here — so if a
	// previously-eligible user becomes ineligible (label change),
	// its ccache is pruned by the normal prune-stale path.
	inRoster := make(map[int]struct{}, len(reqs))
	for _, req := range reqs {
		inRoster[req.UID] = struct{}{}
	}
	// minted tracks UIDs we successfully minted this sweep, used to
	// grow the managed set.
	minted := make(map[int]struct{}, len(reqs))
	for _, req := range reqs {
		if _, err := os.Stat(req.KeytabPath); errors.Is(err, os.ErrNotExist) {
			p.Logger.Printf("ERROR: Keytab missing for principal %s at %s", req.Principal, req.KeytabPath)
			p.emitKeytabMissing(req)
			fail++
			continue
		}
		if err := p.Minter.Mint(ctx, req); err != nil {
			p.Logger.Printf("ERROR: Failed to mint cache for %s: %v", req.Principal, err)
			p.emitMintFailed(req, err)
			fail++
			continue
		}
		p.Logger.Printf("SUCCESS: Minted ticket cache %s -> %s", req.Principal, req.CachePath)
		p.emitMintSucceeded(req)
		ok++
		minted[req.UID] = struct{}{}
	}

	if p.PruneStale {
		removed := p.pruneStale(inRoster)
		for _, uid := range removed {
			p.Logger.Printf("INFO: Pruned stale ticket cache for uid=%d (no longer in roster)", uid)
			p.emitPruned(uid)
		}
	}

	// Grow the managed set with UIDs we minted this sweep. UIDs that
	// failed to mint stay in whatever state the managed set already had
	// them (either previously managed, or unknown).
	if p.managed == nil {
		p.managed = make(map[int]struct{})
	}
	for uid := range minted {
		p.managed[uid] = struct{}{}
	}

	return ok, fail
}

// adoptPreExisting scans p.CacheDir for `krb5cc_<uid>` files that
// match the naming pattern AND whose UID appears in the CURRENT
// roster. Adopts those UIDs into the managed set so pruneStale can
// clean them up on subsequent sweeps if the user becomes
// ineligible (spec.disabled=true, or spec.nodeSelector no longer
// matches this node).
//
// Safety property preserved: a ccache for a UID NOT in the roster
// is NEVER adopted — same guardrail as the original design (human
// `kinit` files stay untouched). Restricting adoption to UIDs
// currently in the roster is the additional guardrail specific to
// startup: a foreign UID that happens to match the pattern won't
// be adopted unless someone actively configured a Principal CR for
// exactly that UID (in which case the operator owns it anyway).
//
// Returns the list of UIDs adopted, for logging.
func (p *Processor) adoptPreExisting() []int {
	// Parse the roster to learn which UIDs are legitimately ours.
	f, err := os.Open(p.RosterPath)
	if err != nil {
		return nil
	}
	defer f.Close()
	reqs, _ := ParseRoster(f, p.KeytabDir, p.CacheDir)
	inRoster := make(map[int]struct{}, len(reqs))
	for _, r := range reqs {
		inRoster[r.UID] = struct{}{}
	}

	entries, err := os.ReadDir(p.CacheDir)
	if err != nil {
		return nil
	}
	if p.managed == nil {
		p.managed = make(map[int]struct{})
	}
	var adopted []int
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		m := ccacheNameRE.FindStringSubmatch(e.Name())
		if m == nil {
			continue
		}
		uid, err := strconv.Atoi(m[1])
		if err != nil {
			continue
		}
		if _, ok := inRoster[uid]; !ok {
			// Not in current roster → refuse to adopt. Might be a
			// human's ccache; not ours to prune.
			continue
		}
		if _, already := p.managed[uid]; already {
			continue
		}
		p.managed[uid] = struct{}{}
		adopted = append(adopted, uid)
	}
	return adopted
}

// FilterEligibleForNode is the exported entry point the daemon's
// `readyz` subcommand uses to compute which UIDs *should* have
// ccaches on this node. Same semantics as applyNodeFilter but
// available as a public API. Silent — no logging, since readiness
// probes shouldn't fill stdout on every heartbeat.
func (p *Processor) FilterEligibleForNode(_ context.Context, reqs []kerberos.TicketRequest) []kerberos.TicketRequest {
	if p.SelectorsPath == "" {
		return reqs
	}
	selectors, err := LoadSelectorsFile(p.SelectorsPath)
	if err != nil || len(selectors) == 0 {
		return reqs
	}
	nodeLabels, _ := LoadNodeLabelsFile(p.NodeLabelsPath)
	filtered := make([]kerberos.TicketRequest, 0, len(reqs))
	for _, req := range reqs {
		if MatchesNode(req.UID, p.NodeName, selectors, nodeLabels).Applies {
			filtered = append(filtered, req)
		}
	}
	return filtered
}

// applyNodeFilter drops roster entries that don't apply to this
// daemon's node according to per-user nodeSelectors. Both files
// are optional; absent files mean "no restrictions" — i.e., this
// method returns reqs unchanged and every entry is minted.
//
// When present, malformed JSON in either file logs a WARN and
// falls back to "no restrictions" (rather than dropping every
// restricted user on the floor) — the operator will re-write
// the CM correctly on the next reconcile.
func (p *Processor) applyNodeFilter(reqs []kerberos.TicketRequest) []kerberos.TicketRequest {
	// Nothing to do if the operator didn't project selectors.
	if p.SelectorsPath == "" {
		return reqs
	}
	selectors, err := LoadSelectorsFile(p.SelectorsPath)
	if err != nil {
		p.Logger.Printf("WARN: could not load selectors from %s (%v); minting all roster entries", p.SelectorsPath, err)
		return reqs
	}
	if len(selectors) == 0 {
		return reqs
	}
	nodeLabels, err := LoadNodeLabelsFile(p.NodeLabelsPath)
	if err != nil {
		p.Logger.Printf("WARN: could not load node labels from %s (%v); failing closed for selector-restricted users", p.NodeLabelsPath, err)
		// nodeLabels stays nil; MatchesNode will fail-closed for
		// restricted users, which is the safer default.
	}
	if p.NodeName == "" {
		// No downward-API-provided node name — this daemon is
		// probably running in tests or dev outside Kubernetes.
		// Fail closed for restricted users and log once per
		// sweep so misconfiguration is loud.
		p.Logger.Printf("WARN: NODE_NAME env not set; selector-restricted users will be skipped this sweep")
	}
	filtered := make([]kerberos.TicketRequest, 0, len(reqs))
	for _, req := range reqs {
		d := MatchesNode(req.UID, p.NodeName, selectors, nodeLabels)
		if !d.Applies {
			p.Logger.Printf("INFO: uid=%d skipped on this node (%s)", req.UID, d.Reason)
			continue
		}
		filtered = append(filtered, req)
	}
	return filtered
}

// pruneStale removes ccache files whose UID was previously managed by
// this processor but is no longer in the current roster. Returns the
// list of UIDs whose files were removed.
//
// Safety properties:
//   - Only files matching `^krb5cc_<digits>$` are considered.
//   - Only UIDs previously observed in a successful sweep are eligible.
//   - UIDs still in the current roster are never touched.
//   - Missing files and read errors are logged, never fatal.
func (p *Processor) pruneStale(current map[int]struct{}) []int {
	if p.managed == nil {
		return nil
	}
	var removed []int
	for uid := range p.managed {
		if _, still := current[uid]; still {
			continue
		}
		path := filepath.Join(p.CacheDir, fmt.Sprintf("krb5cc_%d", uid))
		if !ccacheNameRE.MatchString(filepath.Base(path)) {
			// Defense-in-depth: never remove anything that doesn't
			// match our naming pattern.
			continue
		}
		if err := p.Remove(path); err != nil {
			if !errors.Is(err, os.ErrNotExist) {
				p.Logger.Printf("WARN: Failed to prune stale ccache %s: %v", path, err)
			}
			delete(p.managed, uid)
			continue
		}
		removed = append(removed, uid)
		delete(p.managed, uid)
	}
	return removed
}

// emit* helpers centralise the nil-guard so every call site
// doesn't have to remember that Events may be unset on Processor
// values that skip NewProcessor (a couple of tests do this).
func (p *Processor) emitMintSucceeded(req kerberos.TicketRequest) {
	if p.Events == nil {
		return
	}
	p.Events.MintSucceeded(req.UID, req.Principal)
}

func (p *Processor) emitMintFailed(req kerberos.TicketRequest, err error) {
	if p.Events == nil {
		return
	}
	p.Events.MintFailed(req.UID, req.Principal, err)
}

func (p *Processor) emitKeytabMissing(req kerberos.TicketRequest) {
	if p.Events == nil {
		return
	}
	p.Events.KeytabMissing(req.UID, req.Principal, req.KeytabPath)
}

func (p *Processor) emitPruned(uid int) {
	if p.Events == nil {
		return
	}
	p.Events.Pruned(uid)
}

// StartLoop runs the polling loop until the context is cancelled.
func (p *Processor) StartLoop(ctx context.Context, renewMinutes int) error {
	p.Logger.Printf("INFO: Starting engine monitoring loop over roster: %s", p.RosterPath)

	lastHash, err := p.hashInputs()
	if err != nil {
		return fmt.Errorf("cannot initialize roster file hash: %w", err)
	}

	// v0.8: adopt pre-existing ccaches from a previous daemon
	// process (image rollout, node reboot, etc.). Any ccache file
	// on disk matching /tmp/krb5cc_<uid> whose UID is CURRENTLY in
	// the roster is claimed as "ours" so pruneStale can clean it
	// up when the roster changes or the user becomes ineligible
	// on this node (spec.nodeSelector transition). Foreign UIDs
	// (a human `kinit` on the node) are never adopted because they
	// won't be in the roster — same safety property as the
	// original design.
	if adopted := p.adoptPreExisting(); len(adopted) > 0 {
		p.Logger.Printf("INFO: Adopted %d pre-existing ccache(s) as managed: uids=%v", len(adopted), adopted)
	}

	ok, fail := p.ProcessRoster(ctx)
	p.Logger.Printf("INFO: Initial execution baseline complete (ok=%d, fail=%d)", ok, fail)

	poll := p.Poll
	if poll <= 0 {
		poll = DefaultPollInterval
	}
	ticker := time.NewTicker(poll)
	defer ticker.Stop()

	renewDuration := time.Duration(renewMinutes) * time.Minute
	lastRenewTime := time.Now()

	for {
		select {
		case <-ctx.Done():
			p.Logger.Printf("INFO: shutdown signal received, exiting loop")
			return ctx.Err()
		case <-ticker.C:
			currentHash, err := p.hashInputs()
			if err != nil {
				p.Logger.Printf("WARN: Roster read error during loop interval check: %v", err)
				continue
			}
			if currentHash != lastHash {
				p.Logger.Println("INFO: Dynamic ConfigMap event detected! Refreshing roster immediately...")
				ok, fail = p.ProcessRoster(ctx)
				p.Logger.Printf("INFO: Active configuration sweep complete (ok=%d, fail=%d)", ok, fail)
				lastHash = currentHash
				lastRenewTime = time.Now()
				continue
			}
			if time.Since(lastRenewTime) >= renewDuration {
				p.Logger.Println("INFO: Scheduled ticket lifetime evaluation boundary reached. Running sync step...")
				ok, fail = p.ProcessRoster(ctx)
				p.Logger.Printf("INFO: Routine ticket refreshment sync complete (ok=%d, fail=%d)", ok, fail)
				lastRenewTime = time.Now()
			}
		}
	}
}

// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package watcher

import (
	"os"
	"path/filepath"
	"testing"
)

func TestMatchesNode_Unrestricted(t *testing.T) {
	// A UID absent from selectors is unrestricted → Applies.
	d := MatchesNode(2001, "worker-1", SelectorsMap{}, NodeLabelsMap{})
	if !d.Applies {
		t.Errorf("unrestricted uid should Apply, got %+v", d)
	}
}

func TestMatchesNode_MatchingSelector(t *testing.T) {
	sel := SelectorsMap{2001: {"zone": "prod"}}
	labels := NodeLabelsMap{"worker-1": {"zone": "prod", "tier": "gpu"}}
	d := MatchesNode(2001, "worker-1", sel, labels)
	if !d.Applies {
		t.Errorf("matching selector should Apply, got %+v", d)
	}
}

func TestMatchesNode_SelectorMismatch(t *testing.T) {
	sel := SelectorsMap{2001: {"zone": "prod"}}
	labels := NodeLabelsMap{"worker-1": {"zone": "dev"}}
	d := MatchesNode(2001, "worker-1", sel, labels)
	if d.Applies {
		t.Errorf("mismatched selector should NOT Apply, got %+v", d)
	}
}

func TestMatchesNode_MultipleLabelsAllMustMatch(t *testing.T) {
	// AND semantics: every key=value in the selector must be present
	// in the node's labels. Missing one key = mismatch.
	sel := SelectorsMap{2001: {"zone": "prod", "tier": "gpu"}}
	tests := []struct {
		name    string
		labels  map[string]string
		applies bool
	}{
		{"both match", map[string]string{"zone": "prod", "tier": "gpu"}, true},
		{"missing tier", map[string]string{"zone": "prod"}, false},
		{"wrong tier", map[string]string{"zone": "prod", "tier": "cpu"}, false},
		{"empty labels", map[string]string{}, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			d := MatchesNode(2001, "worker-1", sel, NodeLabelsMap{"worker-1": tt.labels})
			if d.Applies != tt.applies {
				t.Errorf("Applies=%v want %v (reason=%s)", d.Applies, tt.applies, d.Reason)
			}
		})
	}
}

func TestMatchesNode_MissingNodeLabelsFailsClosed(t *testing.T) {
	// If the node's labels aren't in node-labels.json — could be
	// transient operator/kubelet skew — fail closed so we don't
	// leak a ccache to a node we can't verify.
	sel := SelectorsMap{2001: {"zone": "prod"}}
	labels := NodeLabelsMap{"other-node": {"zone": "prod"}}
	d := MatchesNode(2001, "worker-1", sel, labels)
	if d.Applies {
		t.Errorf("unknown-node selector-restricted uid should FAIL closed, got %+v", d)
	}
}

func TestMatchesNode_EmptyNodeName(t *testing.T) {
	// nodeName == "" means no downward API — fail closed for
	// restricted users (same as missing node-labels entry).
	sel := SelectorsMap{2001: {"zone": "prod"}}
	labels := NodeLabelsMap{"worker-1": {"zone": "prod"}}
	d := MatchesNode(2001, "", sel, labels)
	if d.Applies {
		t.Errorf("empty nodeName with restricted user should FAIL closed, got %+v", d)
	}
}

func TestLoadSelectorsFile_Absent(t *testing.T) {
	// Missing file is not an error — the caller treats nil as
	// "no restrictions."
	sel, err := LoadSelectorsFile(filepath.Join(t.TempDir(), "does-not-exist.json"))
	if err != nil {
		t.Errorf("missing file should not be an error, got %v", err)
	}
	if sel != nil {
		t.Errorf("missing file should return nil map, got %v", sel)
	}
}

func TestLoadSelectorsFile_ParsesUIDsAsInts(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "selectors.json")
	if err := writeFile(path, `{"2001":{"zone":"prod"},"2002":{"tier":"gpu"}}`); err != nil {
		t.Fatal(err)
	}
	sel, err := LoadSelectorsFile(path)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if _, ok := sel[2001]; !ok {
		t.Errorf("expected uid 2001 in %v", sel)
	}
	if sel[2001]["zone"] != "prod" {
		t.Errorf("expected zone=prod, got %+v", sel[2001])
	}
}

func TestLoadSelectorsFile_SkipsMalformedUIDs(t *testing.T) {
	// A garbage uid key shouldn't take out the whole filter.
	dir := t.TempDir()
	path := filepath.Join(dir, "selectors.json")
	if err := writeFile(path, `{"2001":{"zone":"prod"},"not-a-number":{"tier":"gpu"}}`); err != nil {
		t.Fatal(err)
	}
	sel, err := LoadSelectorsFile(path)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if _, ok := sel[2001]; !ok {
		t.Errorf("expected uid 2001 to survive despite the malformed peer, got %v", sel)
	}
	if len(sel) != 1 {
		t.Errorf("expected exactly one valid entry, got %v", sel)
	}
}

func writeFile(path, contents string) error {
	return os.WriteFile(path, []byte(contents), 0o600)
}

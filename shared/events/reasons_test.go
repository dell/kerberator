// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package events

import "testing"

func TestKnownReasons_HasAllFour(t *testing.T) {
	for _, r := range []string{
		ReasonMintSucceeded,
		ReasonMintFailed,
		ReasonCcachePruned,
		ReasonKeytabMissing,
	} {
		if _, ok := KnownReasons[r]; !ok {
			t.Errorf("KnownReasons missing %s", r)
		}
	}
	if len(KnownReasons) != 4 {
		t.Errorf("KnownReasons size drift: %d entries", len(KnownReasons))
	}
}

func TestShortReason(t *testing.T) {
	cases := map[string]string{
		ReasonMintSucceeded: "Minted",
		ReasonMintFailed:    "MintFailed",
		ReasonCcachePruned:  "Pruned",
		ReasonKeytabMissing: "KeytabMissing",
		"SomeFutureReason":  "SomeFutureReason", // round-trip unknown
	}
	for in, want := range cases {
		if got := ShortReason(in); got != want {
			t.Errorf("ShortReason(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestUIDAnnotationKey_HasKerberatorGroup(t *testing.T) {
	// Guard against an accidental revert to the pre-rename annotation domain
	// prefix during any future rename.
	want := "kerberator.dell.com/uid"
	if UIDAnnotationKey != want {
		t.Errorf("UIDAnnotationKey = %q, want %q", UIDAnnotationKey, want)
	}
}

// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package events

import (
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestParseUID(t *testing.T) {
	cases := []struct {
		msg  string
		want int64
		ok   bool
	}{
		{"minted principal p@TENANT uid=1000 on node n1", 1000, true},
		{"failed uid=42: reason X", 42, true},
		{"uid=0 nonsense", 0, true},
		{"trailing uid=999", 999, true},
		{"no uid here", 0, false},
		{"uid=notanumber", 0, false},
		{"partial match hostname uid1000", 0, false},
		{"", 0, false},
	}
	for _, c := range cases {
		got, ok := ParseUID(c.msg)
		if ok != c.ok || got != c.want {
			t.Errorf("ParseUID(%q) = (%d, %v), want (%d, %v)", c.msg, got, ok, c.want, c.ok)
		}
	}
}

func TestShortReason(t *testing.T) {
	if ShortReason(ReasonMintSucceeded) != "Minted" {
		t.Error("MintSucceeded short form")
	}
	if ShortReason(ReasonMintFailed) != "MintFailed" {
		t.Error("MintFailed short form")
	}
	if ShortReason(ReasonCcachePruned) != "Pruned" {
		t.Error("CcachePruned short form")
	}
	if ShortReason(ReasonKeytabMissing) != "KeytabMissing" {
		t.Error("KeytabMissing short form")
	}
	if got := ShortReason("SomeFutureReason"); got != "SomeFutureReason" {
		t.Errorf("unknown reason should round-trip: got %q", got)
	}
}

// TestParseUIDFromEvent exercises the two-tier lookup:
//
//   - annotation present + valid -> use it (grep is skipped).
//   - annotation absent -> fall back to grep-parsing Message.
//   - annotation present but bogus -> fail closed (do NOT fall back
//     to grep, because a bogus annotation is a bug we want visible).
//   - both absent -> (0, false).
func TestParseUIDFromEvent(t *testing.T) {
	cases := []struct {
		name    string
		event   *corev1.Event
		wantUID int64
		wantOK  bool
	}{
		{
			name:    "nil event",
			event:   nil,
			wantUID: 0, wantOK: false,
		},
		{
			name: "annotation wins over message",
			event: &corev1.Event{
				ObjectMeta: metav1.ObjectMeta{
					Annotations: map[string]string{UIDAnnotationKey: "2001"},
				},
				Message: "Minted ccache for uid=9999 principal=x",
			},
			wantUID: 2001, wantOK: true,
		},
		{
			name: "annotation absent; fall back to message",
			event: &corev1.Event{
				Message: "Minted ccache for uid=3003 principal=y",
			},
			wantUID: 3003, wantOK: true,
		},
		{
			name: "annotation malformed; fail closed",
			event: &corev1.Event{
				ObjectMeta: metav1.ObjectMeta{
					Annotations: map[string]string{UIDAnnotationKey: "not-a-number"},
				},
				Message: "Minted ccache for uid=4004",
			},
			wantUID: 0, wantOK: false,
		},
		{
			name: "neither annotation nor uid= in message",
			event: &corev1.Event{
				Message: "some unrelated event",
			},
			wantUID: 0, wantOK: false,
		},
		{
			name: "empty annotation string; fail closed",
			event: &corev1.Event{
				ObjectMeta: metav1.ObjectMeta{
					Annotations: map[string]string{UIDAnnotationKey: ""},
				},
				Message: "Minted ccache for uid=5005",
			},
			wantUID: 0, wantOK: false,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, ok := ParseUIDFromEvent(c.event)
			if ok != c.wantOK || got != c.wantUID {
				t.Errorf("ParseUIDFromEvent = (%d, %v), want (%d, %v)",
					got, ok, c.wantUID, c.wantOK)
			}
		})
	}
}

func TestKnownReasons_HasAllFour(t *testing.T) {
	for _, r := range []string{ReasonMintSucceeded, ReasonMintFailed, ReasonCcachePruned, ReasonKeytabMissing} {
		if _, ok := KnownReasons[r]; !ok {
			t.Errorf("KnownReasons missing %s", r)
		}
	}
	if len(KnownReasons) != 4 {
		t.Errorf("KnownReasons size drift: %d entries", len(KnownReasons))
	}
}

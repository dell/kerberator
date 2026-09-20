// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package tenant

import (
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

func mkPrincipal(name string, uid int64, principal, keytab string) v1alpha1.Principal {
	return v1alpha1.Principal{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec: v1alpha1.PrincipalSpec{
			UID:       uid,
			Principal: principal,
			Keytab:    keytab,
		},
	}
}

func TestAggregate_HappyPath(t *testing.T) {
	// Two principals, distinct UIDs, both resolve. Assert:
	//  - roster is sorted by UID
	//  - both keytab bytes are present at the right key
	//  - hash is deterministic and non-empty
	//  - all verdicts Included
	principals := []v1alpha1.Principal{
		mkPrincipal("uid-2002", 2002, "svc-b@TENANT", "svc-b.keytab"),
		mkPrincipal("uid-2001", 2001, "svc-a@TENANT", "svc-a.keytab"),
	}
	src := []KeytabSource{
		{PrincipalName: "uid-2001", Filename: "svc-a.keytab", Bytes: []byte("A")},
		{PrincipalName: "uid-2002", Filename: "svc-b.keytab", Bytes: []byte("BB")},
	}
	agg := Aggregate(principals, src)

	want := "2001:svc-a@TENANT:svc-a.keytab\n2002:svc-b@TENANT:svc-b.keytab\n"
	if agg.RosterBlob != want {
		t.Errorf("roster blob mismatch:\ngot:  %q\nwant: %q", agg.RosterBlob, want)
	}
	if len(agg.KeytabData) != 2 {
		t.Errorf("expected 2 keytab entries, got %d", len(agg.KeytabData))
	}
	if string(agg.KeytabData["svc-a.keytab"]) != "A" || string(agg.KeytabData["svc-b.keytab"]) != "BB" {
		t.Errorf("keytab data mismatch: %v", agg.KeytabData)
	}
	if !strings.HasPrefix(agg.RosterHash, "sha256:") {
		t.Errorf("expected sha256: prefix, got %q", agg.RosterHash)
	}
	if len(agg.Verdicts) != 2 {
		t.Fatalf("expected 2 verdicts, got %d", len(agg.Verdicts))
	}
	for _, v := range agg.Verdicts {
		if !v.Included || v.Rejected {
			t.Errorf("expected included, got %+v", v)
		}
	}
	if len(agg.DuplicateUIDs) != 0 {
		t.Errorf("expected no duplicates, got %v", agg.DuplicateUIDs)
	}
}

func TestAggregate_DeterministicHash(t *testing.T) {
	// Input order shouldn't affect the output.
	users1 := []v1alpha1.Principal{
		mkPrincipal("uid-2001", 2001, "svc-a@TENANT", "svc-a.keytab"),
		mkPrincipal("uid-2002", 2002, "svc-b@TENANT", "svc-b.keytab"),
	}
	users2 := []v1alpha1.Principal{
		mkPrincipal("uid-2002", 2002, "svc-b@TENANT", "svc-b.keytab"),
		mkPrincipal("uid-2001", 2001, "svc-a@TENANT", "svc-a.keytab"),
	}
	src := []KeytabSource{
		{PrincipalName: "uid-2001", Filename: "svc-a.keytab", Bytes: []byte("A")},
		{PrincipalName: "uid-2002", Filename: "svc-b.keytab", Bytes: []byte("BB")},
	}
	if Aggregate(users1, src).RosterHash != Aggregate(users2, src).RosterHash {
		t.Errorf("hash depends on input order — that's a determinism bug")
	}
}

func TestAggregate_DuplicateUID(t *testing.T) {
	principals := []v1alpha1.Principal{
		mkPrincipal("uid-2001-a", 2001, "svc-a@TENANT", "svc-a.keytab"),
		mkPrincipal("uid-2001-b", 2001, "svc-b@TENANT", "svc-b.keytab"),
	}
	src := []KeytabSource{
		{PrincipalName: "uid-2001-a", Filename: "svc-a.keytab", Bytes: []byte("A")},
		{PrincipalName: "uid-2001-b", Filename: "svc-b.keytab", Bytes: []byte("B")},
	}
	agg := Aggregate(principals, src)
	if agg.RosterBlob != "" {
		t.Errorf("expected empty roster on dup, got %q", agg.RosterBlob)
	}
	if len(agg.DuplicateUIDs) != 1 || agg.DuplicateUIDs[0] != 2001 {
		t.Errorf("expected DuplicateUIDs=[2001], got %v", agg.DuplicateUIDs)
	}
	for _, v := range agg.Verdicts {
		if !v.Rejected || v.Reason != "DuplicateUID" {
			t.Errorf("expected DuplicateUID reject, got %+v", v)
		}
	}
}

func TestAggregate_MissingKeytab(t *testing.T) {
	principals := []v1alpha1.Principal{
		mkPrincipal("uid-2001", 2001, "svc-a@TENANT", "svc-a.keytab"),
		mkPrincipal("uid-2002", 2002, "svc-b@TENANT", "svc-b.keytab"),
	}
	// only 2001 resolves
	src := []KeytabSource{
		{PrincipalName: "uid-2001", Filename: "svc-a.keytab", Bytes: []byte("A")},
	}
	agg := Aggregate(principals, src)
	if !strings.Contains(agg.RosterBlob, "2001:") || strings.Contains(agg.RosterBlob, "2002:") {
		t.Errorf("expected only uid-2001 in roster, got %q", agg.RosterBlob)
	}
	var got2002 PrincipalVerdict
	for _, v := range agg.Verdicts {
		if v.PrincipalName == "uid-2002" {
			got2002 = v
		}
	}
	if !got2002.Rejected || got2002.Reason != "KeytabSecretMissing" {
		t.Errorf("expected KeytabSecretMissing for uid-2002, got %+v", got2002)
	}
}

func TestAggregate_EmptyInput(t *testing.T) {
	agg := Aggregate(nil, nil)
	if agg.RosterBlob != "" {
		t.Errorf("expected empty roster, got %q", agg.RosterBlob)
	}
	if len(agg.KeytabData) != 1 {
		t.Errorf("expected placeholder in KeytabData, got %v", agg.KeytabData)
	}
	if _, ok := agg.KeytabData[".placeholder"]; !ok {
		t.Errorf("expected .placeholder key, got %v", agg.KeytabData)
	}
	if agg.RosterHash == "" {
		t.Errorf("expected hash of empty string, got empty")
	}
}

func TestAggregate_KeytabFilenameCollision(t *testing.T) {
	// Two distinct UIDs but colliding filename + different bytes.
	principals := []v1alpha1.Principal{
		mkPrincipal("uid-2001", 2001, "svc-a@TENANT", "shared.keytab"),
		mkPrincipal("uid-2002", 2002, "svc-b@TENANT", "shared.keytab"),
	}
	src := []KeytabSource{
		{PrincipalName: "uid-2001", Filename: "shared.keytab", Bytes: []byte("A")},
		{PrincipalName: "uid-2002", Filename: "shared.keytab", Bytes: []byte("B")},
	}
	agg := Aggregate(principals, src)
	// Both principals should be rejected with KeytabFilenameCollision.
	// Roster should be empty.
	if agg.RosterBlob != "" {
		t.Errorf("expected empty roster on filename collision, got %q", agg.RosterBlob)
	}
	for _, v := range agg.Verdicts {
		if !v.Rejected || v.Reason != "KeytabFilenameCollision" {
			t.Errorf("expected KeytabFilenameCollision, got %+v", v)
		}
	}
}

func TestAggregate_SameFilenameSameBytes(t *testing.T) {
	// Two principals happen to share a filename AND bytes (e.g., they
	// share a keytab file for the same principal). This is legal
	// and both should be included.
	principals := []v1alpha1.Principal{
		mkPrincipal("uid-2001", 2001, "svc-a@TENANT", "shared.keytab"),
		mkPrincipal("uid-2002", 2002, "svc-b@TENANT", "shared.keytab"),
	}
	src := []KeytabSource{
		{PrincipalName: "uid-2001", Filename: "shared.keytab", Bytes: []byte("SAME")},
		{PrincipalName: "uid-2002", Filename: "shared.keytab", Bytes: []byte("SAME")},
	}
	agg := Aggregate(principals, src)
	if !strings.Contains(agg.RosterBlob, "2001:") || !strings.Contains(agg.RosterBlob, "2002:") {
		t.Errorf("expected both principals in roster, got %q", agg.RosterBlob)
	}
	if len(agg.KeytabData) != 1 {
		t.Errorf("expected single dedup'd keytab entry, got %d: %v", len(agg.KeytabData), agg.KeytabData)
	}
	for _, v := range agg.Verdicts {
		if !v.Included {
			t.Errorf("expected included, got %+v", v)
		}
	}
}

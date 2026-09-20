// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package keytab

import (
	"encoding/base64"
	"errors"
	"strings"
	"testing"
)

// fixtureKeytabBase64 is a synthetic MIT keytab (format 0x0502)
// generated with `ktutil addent -password` from a throwaway random
// password. It guards against subtle mis-parses of the on-disk
// format; the key material is meaningless.
//
//	klist -e -k on the same bytes reports:
//	  KVNO 3 · svc-2001@EXAMPLE.COM
//	  4 enctypes: aes{128,256}-cts-hmac-sha1-96,
//	              aes{128,256}-cts-hmac-sha{256-128,384-192}
const fixtureKeytabBase64 = `BQIAAAA6AAEAC0VYQU1QTEUuQ09NAAhzdmMtMjAwMQAAAAFqsEpuAwARABDcXipRjo1+fOtYicCrvWZ5AAAAAwAAAEoAAQALRVhBTVBMRS5DT00ACHN2Yy0yMDAxAAAAAWqwSm4DABIAIOECd5L2xMaiucO+Zj6WmuPClzlqMTsgxvsF/U+R9J/9AAAAAwAAADoAAQALRVhBTVBMRS5DT00ACHN2Yy0yMDAxAAAAAWqwSm4DABMAEG7LDK4T7m46nIQ4o65tOrAAAAADAAAASgABAAtFWEFNUExFLkNPTQAIc3ZjLTIwMDEAAAABarBKbgMAFAAgL60kF+V673e4mdeA2MErpkABOvJ/sfuHkVumYcmljbUAAAAD`

func mustDecodeFixtureKeytab(t *testing.T) []byte {
	t.Helper()
	b, err := base64.StdEncoding.DecodeString(fixtureKeytabBase64)
	if err != nil {
		t.Fatalf("decode fixture: %v", err)
	}
	return b
}

func TestParse_FixtureKeytab_v0502(t *testing.T) {
	data := mustDecodeFixtureKeytab(t)
	if got, want := len(data), 282; got != want {
		t.Fatalf("fixture size drift: got %d bytes, want %d", got, want)
	}

	kt, err := Parse(data)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}

	if kt.Version != 0x0502 {
		t.Errorf("Version: got 0x%04x, want 0x0502", kt.Version)
	}
	if got, want := len(kt.Entries), 4; got != want {
		t.Fatalf("entries: got %d, want %d", got, want)
	}
	if got, want := kt.MaxKvno(), uint32(3); got != want {
		t.Errorf("MaxKvno: got %d, want %d", got, want)
	}
	if got, want := len(kt.Principals()), 1; got != want {
		t.Errorf("Principals count: got %d, want %d", got, want)
	}
	if got, want := kt.Principals()[0], "svc-2001@EXAMPLE.COM"; got != want {
		t.Errorf("Principal: got %q, want %q", got, want)
	}

	// Every entry should share the KVNO for a freshly-rotated
	// principal.
	for i, e := range kt.Entries {
		if e.Kvno != 3 {
			t.Errorf("entry %d: Kvno=%d, want 3", i, e.Kvno)
		}
		if !strings.HasSuffix(e.Principal, "@EXAMPLE.COM") {
			t.Errorf("entry %d: Principal=%q, want @EXAMPLE.COM realm", i, e.Principal)
		}
	}

	// Enctypes: aes128-cts-hmac-sha1-96 (17), aes256-cts-hmac-sha1-96 (18),
	// aes128-cts-hmac-sha256-128 (19), aes256-cts-hmac-sha384-192 (20).
	wantEnctypes := []string{
		"aes128-cts-hmac-sha1-96",
		"aes128-cts-hmac-sha256-128",
		"aes256-cts-hmac-sha1-96",
		"aes256-cts-hmac-sha384-192",
	}
	gotEnctypes := kt.Enctypes()
	if len(gotEnctypes) != len(wantEnctypes) {
		t.Fatalf("Enctypes: got %v, want %v", gotEnctypes, wantEnctypes)
	}
	for i, w := range wantEnctypes {
		if gotEnctypes[i] != w {
			t.Errorf("Enctypes[%d]: got %q, want %q", i, gotEnctypes[i], w)
		}
	}
}

func TestParse_Empty(t *testing.T) {
	_, err := Parse(nil)
	if !errors.Is(err, ErrEmpty) {
		t.Errorf("nil: got %v, want ErrEmpty", err)
	}
	_, err = Parse([]byte{})
	if !errors.Is(err, ErrEmpty) {
		t.Errorf("empty: got %v, want ErrEmpty", err)
	}
}

func TestParse_UnsupportedVersion(t *testing.T) {
	// v1 magic
	_, err := Parse([]byte{0x05, 0x01})
	if !errors.Is(err, ErrUnsupportedVersion) {
		t.Errorf("v1: got %v, want ErrUnsupportedVersion", err)
	}
	// nonsense magic
	_, err = Parse([]byte{0xff, 0xff, 0x00, 0x00})
	if !errors.Is(err, ErrUnsupportedVersion) {
		t.Errorf("bogus: got %v, want ErrUnsupportedVersion", err)
	}
}

func TestParse_TruncatedHeader(t *testing.T) {
	_, err := Parse([]byte{0x05}) // only one byte, need two for the magic
	if !errors.Is(err, ErrTruncated) {
		t.Errorf("got %v, want ErrTruncated", err)
	}
}

func TestParse_TruncatedEntry(t *testing.T) {
	// Valid magic followed by a claim of 200 bytes but only 10
	// bytes of actual entry data.
	data := []byte{0x05, 0x02, 0x00, 0x00, 0x00, 0xC8, 0x00, 0x01, 'A', 'B', 'C', 'D', 'E', 'F'}
	_, err := Parse(data)
	if !errors.Is(err, ErrTruncated) {
		t.Errorf("got %v, want ErrTruncated", err)
	}
}

func TestParse_DeletedEntrySkipped(t *testing.T) {
	// A "deleted" entry has a negative length prefix. We should
	// skip its body and continue parsing the next real entry.
	fixture := mustDecodeFixtureKeytab(t)

	// Craft: magic + one deleted entry of 10 bytes (all zeros) +
	// the 282-byte fixture body starting after the magic.
	// Deleted length is int32(-10) = 0xFFFFFFF6.
	var b []byte
	b = append(b, fixture[:2]...)         // magic
	b = append(b, 0xFF, 0xFF, 0xFF, 0xF6) // len = -10
	b = append(b, make([]byte, 10)...)    // 10 bytes of garbage
	b = append(b, fixture[2:]...)         // fixture entries after magic

	kt, err := Parse(b)
	if err != nil {
		t.Fatalf("Parse with deleted entry: %v", err)
	}
	if got, want := len(kt.Entries), 4; got != want {
		t.Errorf("entries: got %d, want %d (deleted entry should be skipped)", got, want)
	}
	if got, want := kt.MaxKvno(), uint32(3); got != want {
		t.Errorf("MaxKvno: got %d, want %d", got, want)
	}
}

func TestEnctypeName_UnknownFallsThrough(t *testing.T) {
	// A safety property: if FreeIPA ever starts issuing a new
	// etype we haven't hard-coded, the parser must NOT fail —
	// it must fall through to "etype-<id>" so operators can
	// still see the KVNO.
	if got := enctypeName(99); got != "etype-99" {
		t.Errorf("unknown etype: got %q, want etype-99", got)
	}
	// Known etypes should still resolve.
	if got := enctypeName(18); got != "aes256-cts-hmac-sha1-96" {
		t.Errorf("etype 18: got %q, want aes256-cts-hmac-sha1-96", got)
	}
}

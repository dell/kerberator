// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// Package keytab implements a minimal parser for MIT-format
// Kerberos keytab files (version 0x0502). It exists to let the
// operator and CLI surface the KVNO and enctypes stored in a
// principal's referenced Keytab Secret without pulling in a full
// Kerberos library.
//
// Scope: read-only introspection of KVNO and enctypes. We do not
// re-encode keytabs, we do not resolve principals against a KDC,
// and we intentionally ignore the actual key bytes — the caller
// gets back just enough metadata to detect drift and label the
// current key generation in status.
//
// Format reference:
//
//	https://web.mit.edu/kerberos/krb5-latest/doc/formats/keytab_file_format.html
//
// Byte order is big-endian throughout. The MIT v2 format (first
// two bytes = 0x0502) is the only format Kerberator has ever
// seen in the wild from FreeIPA and MIT KDCs; we reject v1 and
// unknown versions rather than guess.
package keytab

import (
	"encoding/binary"
	"errors"
	"fmt"
	"sort"
	"strings"
)

// Keytab is the parsed representation of one keytab file.
type Keytab struct {
	// Version is the two-byte magic — always 0x0502 for the
	// files we accept. Preserved so callers can log it if they
	// ever start seeing surprises.
	Version uint16
	// Entries in the order they appeared in the file, minus any
	// deleted entries (which we skip).
	Entries []Entry
}

// Entry describes one key stored in the keytab.
//
// A single principal can have multiple entries — typically one
// per encryption type. Rotating the key produces new entries
// with a bumped Kvno.
type Entry struct {
	Principal   string // e.g. "svc-2001@EXAMPLE.COM"
	Timestamp   uint32 // when this key was minted, seconds since epoch
	Kvno        uint32 // MIT calls this the "key version number"
	EnctypeID   uint16 // Kerberos etype ID (see RFC 3961 §8)
	EnctypeName string // human name, "aes256-cts-hmac-sha1-96" style; "etype-<id>" for unknowns
}

// MaxKvno returns the highest KVNO across all entries.
//
// This matches what the `kvno` command-line tool reports for a
// principal: the freshest key generation stored in the file.
// Returns 0 when there are no entries.
func (k *Keytab) MaxKvno() uint32 {
	var max uint32
	for _, e := range k.Entries {
		if e.Kvno > max {
			max = e.Kvno
		}
	}
	return max
}

// Enctypes returns the deduplicated set of enctype names present
// in the keytab, sorted alphabetically for stable output.
func (k *Keytab) Enctypes() []string {
	seen := make(map[string]struct{}, len(k.Entries))
	for _, e := range k.Entries {
		seen[e.EnctypeName] = struct{}{}
	}
	out := make([]string, 0, len(seen))
	for name := range seen {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// EnctypesString returns Enctypes() joined with ", " — a stable
// human/CRD-friendly rendering.
func (k *Keytab) EnctypesString() string {
	return strings.Join(k.Enctypes(), ", ")
}

// Principals returns the deduplicated set of principal names in
// the keytab, sorted. In Kerberator's model there's one principal
// per keytab, but the format allows multiple; expose it so we can
// detect the surprise case.
func (k *Keytab) Principals() []string {
	seen := make(map[string]struct{}, len(k.Entries))
	for _, e := range k.Entries {
		seen[e.Principal] = struct{}{}
	}
	out := make([]string, 0, len(seen))
	for name := range seen {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

var (
	// ErrEmpty is returned when the input is a zero-length
	// slice. Callers hit this before the Secret has any actual
	// keytab bytes (e.g., initial Secret creation with an empty
	// data key).
	ErrEmpty = errors.New("keytab: input is empty")
	// ErrTruncated means the file ended mid-entry.
	ErrTruncated = errors.New("keytab: truncated")
	// ErrUnsupportedVersion means the two-byte magic wasn't
	// 0x0502. We keep the parser strict rather than guessing at
	// v1 (0x0501) layout, which nothing in Kerberator's target
	// deployments produces.
	ErrUnsupportedVersion = errors.New("keytab: unsupported version")
)

// Parse decodes a keytab from raw bytes. Deleted entries
// (negative record size) are skipped without being surfaced.
//
// The parser is tolerant of the two trailing-bytes variant of
// v2 entries: an optional 32-bit KVNO after the key blob. When
// present it wins over the mandatory 8-bit KVNO earlier in the
// record. When absent (older keytabs), the 8-bit KVNO is used
// as-is.
func Parse(data []byte) (*Keytab, error) {
	if len(data) == 0 {
		return nil, ErrEmpty
	}
	if len(data) < 2 {
		return nil, ErrTruncated
	}
	kt := &Keytab{
		Version: binary.BigEndian.Uint16(data[:2]),
	}
	if kt.Version != 0x0502 {
		return nil, fmt.Errorf("%w: got 0x%04x, want 0x0502", ErrUnsupportedVersion, kt.Version)
	}

	pos := 2
	for pos < len(data) {
		if pos+4 > len(data) {
			return nil, fmt.Errorf("%w: entry length at offset %d", ErrTruncated, pos)
		}
		// Record length: int32. Negative = deleted entry;
		// magnitude of the size tells us how many bytes to skip.
		rawLen := int32(binary.BigEndian.Uint32(data[pos : pos+4])) //nolint:gosec // bounds checked above
		pos += 4
		if rawLen == 0 {
			// End-of-file marker some keytab writers emit.
			break
		}
		var recLen int
		if rawLen < 0 {
			recLen = int(-rawLen)
		} else {
			recLen = int(rawLen)
		}
		if pos+recLen > len(data) {
			return nil, fmt.Errorf("%w: entry body at offset %d (need %d, have %d)",
				ErrTruncated, pos, recLen, len(data)-pos)
		}
		body := data[pos : pos+recLen]
		pos += recLen

		if rawLen < 0 {
			// Deleted — skip without producing an Entry.
			continue
		}

		e, err := parseEntry(body)
		if err != nil {
			return nil, fmt.Errorf("keytab: entry at offset %d: %w", pos-recLen, err)
		}
		kt.Entries = append(kt.Entries, e)
	}
	return kt, nil
}

// parseEntry decodes one non-deleted entry body (i.e., the bytes
// after the record-length int32).
func parseEntry(body []byte) (Entry, error) {
	var e Entry
	off := 0

	// num_components: int16
	if off+2 > len(body) {
		return e, fmt.Errorf("num_components: %w", ErrTruncated)
	}
	numComps := int(binary.BigEndian.Uint16(body[off : off+2]))
	off += 2

	// realm: counted_octet_string (int16 length + bytes)
	realm, n, err := readCountedString(body[off:])
	if err != nil {
		return e, fmt.Errorf("realm: %w", err)
	}
	off += n

	// components: numComps × counted_octet_string
	comps := make([]string, 0, numComps)
	for i := 0; i < numComps; i++ {
		c, n, err := readCountedString(body[off:])
		if err != nil {
			return e, fmt.Errorf("component %d: %w", i, err)
		}
		off += n
		comps = append(comps, c)
	}
	e.Principal = strings.Join(comps, "/") + "@" + realm

	// name_type: int32 (v2 only; we already know we're v2)
	if off+4 > len(body) {
		return e, fmt.Errorf("name_type: %w", ErrTruncated)
	}
	// name_type not stored — Kerberator doesn't render it.
	off += 4

	// timestamp: int32
	if off+4 > len(body) {
		return e, fmt.Errorf("timestamp: %w", ErrTruncated)
	}
	e.Timestamp = binary.BigEndian.Uint32(body[off : off+4])
	off += 4

	// vno8: int8
	if off+1 > len(body) {
		return e, fmt.Errorf("vno8: %w", ErrTruncated)
	}
	vno8 := body[off]
	off += 1

	// enctype (keyblock.type): int16
	if off+2 > len(body) {
		return e, fmt.Errorf("enctype: %w", ErrTruncated)
	}
	e.EnctypeID = binary.BigEndian.Uint16(body[off : off+2])
	e.EnctypeName = enctypeName(e.EnctypeID)
	off += 2

	// keylen: int16, then key bytes (skipped — we don't want to
	// touch actual key material in status)
	if off+2 > len(body) {
		return e, fmt.Errorf("keylen: %w", ErrTruncated)
	}
	keyLen := int(binary.BigEndian.Uint16(body[off : off+2]))
	off += 2
	if off+keyLen > len(body) {
		return e, fmt.Errorf("key bytes: %w", ErrTruncated)
	}
	off += keyLen

	// Optional trailing vno32: int32. If four bytes remain (or
	// more, rounding up on padded writers), the first four are
	// the full 32-bit KVNO and override vno8.
	if len(body)-off >= 4 {
		e.Kvno = binary.BigEndian.Uint32(body[off : off+4])
	} else {
		e.Kvno = uint32(vno8)
	}
	return e, nil
}

// readCountedString reads a counted_octet_string (int16 length
// + bytes) from the front of b and returns the string plus the
// number of bytes consumed.
func readCountedString(b []byte) (string, int, error) {
	if len(b) < 2 {
		return "", 0, ErrTruncated
	}
	n := int(binary.BigEndian.Uint16(b[:2]))
	if 2+n > len(b) {
		return "", 0, ErrTruncated
	}
	return string(b[2 : 2+n]), 2 + n, nil
}

// enctypeName maps a Kerberos enctype ID to its canonical name.
// The full IANA registry lives at:
//
//	https://www.iana.org/assignments/kerberos-parameters/kerberos-parameters.xhtml
//
// We only enumerate the enctypes FreeIPA / MIT KDC / Active
// Directory actually issue today. Anything else falls through to
// "etype-<id>" so operators can look it up without the parser
// failing.
func enctypeName(id uint16) string {
	switch id {
	case 1:
		return "des-cbc-crc"
	case 2:
		return "des-cbc-md4"
	case 3:
		return "des-cbc-md5"
	case 5:
		return "des3-cbc-md5"
	case 7:
		return "des3-cbc-sha1"
	case 16:
		return "des3-cbc-sha1-kd"
	case 17:
		return "aes128-cts-hmac-sha1-96"
	case 18:
		return "aes256-cts-hmac-sha1-96"
	case 19:
		return "aes128-cts-hmac-sha256-128"
	case 20:
		return "aes256-cts-hmac-sha384-192"
	case 23:
		return "rc4-hmac"
	case 24:
		return "rc4-hmac-exp"
	case 25:
		return "camellia128-cts-cmac"
	case 26:
		return "camellia256-cts-cmac"
	default:
		return fmt.Sprintf("etype-%d", id)
	}
}

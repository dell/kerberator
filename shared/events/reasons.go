// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// Package events holds the wire contract between the kerberator daemon
// (which emits per-UID Kubernetes Events on its own Pod) and the
// kerberator operator (which ingests those Events into an in-memory
// cache and projects them into `Principal.status.nodes[]`).
//
// The two binaries are separate processes shipped from the same
// repo. Keeping the contract in a dependency-free shared package
// prevents the "reason strings drift between components" bug class
// that we would have suffered with two independent repos.
package events

// Event reasons emitted by the daemon and recognized by the
// operator. Changing any string here requires a coordinated change
// to both binaries; that's a feature, not a bug — this is the whole
// point of having a shared package.
const (
	ReasonMintSucceeded = "KerberosMintSucceeded"
	ReasonMintFailed    = "KerberosMintFailed"
	ReasonCcachePruned  = "KerberosCcachePruned"
	ReasonKeytabMissing = "KerberosKeytabMissing"

	// EventComponentName is the value the daemon publishes as
	// Event.Source.Component. Kept in sync so `kubectl get events`
	// filters on the reporter name work across releases.
	EventComponentName = "kerberator-daemon"
)

// KnownReasons is the set of daemon reasons the operator filters
// on. Sharing this rather than duplicating the four literals
// keeps the "which reasons matter" answer in one place.
var KnownReasons = map[string]struct{}{
	ReasonMintSucceeded: {},
	ReasonMintFailed:    {},
	ReasonCcachePruned:  {},
	ReasonKeytabMissing: {},
}

// UIDAnnotationKey is the Event annotation the daemon stamps with
// the decimal UID this event describes. Consumers prefer this over
// regex-parsing `uid=<N>` from Event.Message: annotations are
// structured, impossible to corrupt with a future message reword,
// and cheap to look up on Event objects.
//
// The `uid=<N>` substring is still emitted in Event.Message for
// humans reading `kubectl describe pod`, but the operator only
// falls back to grep-parsing it if the annotation is absent.
const UIDAnnotationKey = "kerberator.dell.com/uid"

// PrincipalFinalizer is the finalizer string the operator manages on
// Principal CRs. Kept in this shared package so the daemon (which
// doesn't touch CRs directly) still has a canonical reference
// when documentation or scripts need one.
const PrincipalFinalizer = "kerberator.dell.com/ccache-purged"

// ShortReason maps a daemon reason to the short form the operator
// surfaces on Principal.status.nodes[].reason. Unknown reasons
// round-trip unchanged so a future daemon can introduce a new
// reason without a coordinated operator release (the operator
// just carries the new string through until we teach it a nicer
// short form).
func ShortReason(daemonReason string) string {
	switch daemonReason {
	case ReasonMintSucceeded:
		return "Minted"
	case ReasonMintFailed:
		return "MintFailed"
	case ReasonCcachePruned:
		return "Pruned"
	case ReasonKeytabMissing:
		return "KeytabMissing"
	default:
		return daemonReason
	}
}

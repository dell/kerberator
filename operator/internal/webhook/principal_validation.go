// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// Package webhook implements the validating admission webhook
// for Principal CRs. It catches five common misconfigurations
// at apply time so users get a clear error from `kubectl apply`
// rather than a confusing pod-schedule-time failure or a silent
// bad row in the aggregated roster.
//
// The five rules:
//
//  1. spec.uid must be > 0 (0 is reserved for root).
//  2. spec.uid, when the principal's namespace carries an OpenShift
//     SCC UID-range annotation (openshift.io/sa.scc.uid-range),
//     must fall within that range. Non-OpenShift clusters have
//     no annotation and this check is skipped.
//  3. spec.principal must have the `name@TENANT` shape and its
//     tenant portion must equal the referenced Tenant's
//     spec.tenant.
//  4. spec.tenantRef.name is required, and on Create it must
//     reference an existing Tenant in the same
//     namespace. Updates against a since-deleted tenant are
//     allowed so an operator can still `kubectl delete` a
//     stranded principal.
//  5. spec.keytabSecretRef, when set, must specify both a Name
//     and a Key (shape check only; the Secret's existence is
//     verified at reconcile time since Secrets can arrive
//     out-of-order).
//
// The validation logic is intentionally split into a pure function
// (ValidatePrincipal, below) and a thin admission.Handler wrapper
// (Handle in principal_webhook.go). The pure function accepts every
// piece of contextual data it needs (namespace annotations, the
// referenced Tenant, the operation type) so it can
// be exhaustively unit-tested without a fake client. The handler
// is responsible only for extracting those inputs from the
// admission request and translating field.Error slices into an
// admission response.
package webhook

import (
	"fmt"
	"strconv"
	"strings"

	"k8s.io/apimachinery/pkg/util/validation/field"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// SCCUIDRangeAnnotation is the OpenShift-standard namespace
// annotation whose value is a "<start>/<length>" pair (e.g.
// "1000660000/10000"). It's populated automatically on OpenShift
// project creation and is the canonical source of truth for
// "which UIDs may run in this namespace" — validating principal UIDs
// against it prevents a principal CR from ever silently mismatching
// what the underlying container runtime will let pods use.
const SCCUIDRangeAnnotation = "openshift.io/sa.scc.uid-range"

// ValidationOp indicates the admission operation being validated.
// Some rules apply differently to Create vs Update — most notably
// tenantRef existence.
type ValidationOp int

const (
	OpCreate ValidationOp = iota
	OpUpdate
	OpDelete
)

// ValidatePrincipal runs all five rules against the given principal.
// It returns a field.ErrorList (empty when the principal is valid);
// callers translate the list into an admission response.
//
// Inputs beyond the principal itself:
//
//   - nsAnnotations: the principal's namespace metadata.annotations.
//     Pass nil / empty on non-OpenShift clusters or when the
//     annotation lookup failed; the SCC check silently no-ops.
//
//   - tenant: the Tenant referenced by
//     spec.tenantRef.name. Pass nil if the tenant was not
//     found in the API; factoryFound records that so the
//     "unknown tenantRef" rule can flag it on Create.
//
//   - op: OpCreate / OpUpdate / OpDelete. Only OpCreate rejects
//     an unknown tenantRef; OpUpdate tolerates it because an
//     admin might legitimately be editing a principal whose tenant
//     was deleted out from under them (e.g., to strip a
//     finalizer or fix a broken reference).
func ValidatePrincipal(
	principal *v1alpha1.Principal,
	nsAnnotations map[string]string,
	tenant *v1alpha1.Tenant,
	factoryFound bool,
	op ValidationOp,
) field.ErrorList {
	var errs field.ErrorList
	specPath := field.NewPath("spec")

	// Rule 1: UID > 0.
	if principal.Spec.UID <= 0 {
		errs = append(errs, field.Invalid(
			specPath.Child("uid"),
			principal.Spec.UID,
			"uid must be a positive integer; 0 is reserved for root and must never be used for a principal",
		))
	}

	// Rule 2: UID within SCC range (skipped when annotation absent).
	if start, length, ok := parseSCCRange(nsAnnotations); ok {
		if principal.Spec.UID < start || principal.Spec.UID >= start+length {
			errs = append(errs, field.Invalid(
				specPath.Child("uid"),
				principal.Spec.UID,
				fmt.Sprintf(
					"uid %d falls outside the namespace's SCC uid-range [%d, %d); "+
						"the annotation %q on the principal namespace defines the allowed window",
					principal.Spec.UID, start, start+length, SCCUIDRangeAnnotation,
				),
			))
		}
	}

	// Rule 3: principal shape + tenant match.
	principalPath := specPath.Child("principal")
	name, principalRealm, ok := splitPrincipal(principal.Spec.Principal)
	if !ok {
		errs = append(errs, field.Invalid(
			principalPath,
			principal.Spec.Principal,
			"principal must have the shape <name>@<TENANT>; both parts must be non-empty",
		))
	} else if tenant != nil && tenant.Spec.Realm != "" && principalRealm != tenant.Spec.Realm {
		errs = append(errs, field.Invalid(
			principalPath,
			principal.Spec.Principal,
			fmt.Sprintf(
				"principal tenant %q does not match the referenced Tenant %q's spec.tenant %q",
				principalRealm, tenant.Name, tenant.Spec.Realm,
			),
		))
		_ = name // reserved for future name-shape validation
	}

	// Rule 4: tenantRef.name is required, and must resolve on Create.
	tenantRefPath := specPath.Child("tenantRef").Child("name")
	if principal.Spec.TenantRef.Name == "" {
		errs = append(errs, field.Required(
			tenantRefPath,
			"tenantRef.name is required; specify the Tenant this principal belongs to",
		))
	} else if !factoryFound && op == OpCreate {
		errs = append(errs, field.Invalid(
			tenantRefPath,
			principal.Spec.TenantRef.Name,
			fmt.Sprintf(
				"Tenant %q not found in namespace; create the tenant before creating principals that reference it",
				principal.Spec.TenantRef.Name,
			),
		))
	}

	// Rule 5: keytabSecretRef shape.
	if principal.Spec.KeytabSecretRef != nil {
		refPath := specPath.Child("keytabSecretRef")
		if principal.Spec.KeytabSecretRef.Name == "" {
			errs = append(errs, field.Required(
				refPath.Child("name"),
				"keytabSecretRef.name must be set when keytabSecretRef is provided",
			))
		}
		if principal.Spec.KeytabSecretRef.Key == "" {
			errs = append(errs, field.Required(
				refPath.Child("key"),
				"keytabSecretRef.key must be set when keytabSecretRef is provided",
			))
		}
	}

	return errs
}

// parseSCCRange extracts (start, length) from the OpenShift
// SCC uid-range annotation. Returns ok=false when the annotation
// is absent or malformed; malformed values fail closed by simply
// skipping the check, matching upstream OpenShift's own tolerant
// parser. Malformed annotation is not the principal's fault and
// should not block admission.
func parseSCCRange(annotations map[string]string) (start, length int64, ok bool) {
	raw, present := annotations[SCCUIDRangeAnnotation]
	if !present {
		return 0, 0, false
	}
	parts := strings.SplitN(raw, "/", 2)
	if len(parts) != 2 {
		return 0, 0, false
	}
	s, err := strconv.ParseInt(strings.TrimSpace(parts[0]), 10, 64)
	if err != nil {
		return 0, 0, false
	}
	l, err := strconv.ParseInt(strings.TrimSpace(parts[1]), 10, 64)
	if err != nil {
		return 0, 0, false
	}
	if s < 0 || l <= 0 {
		return 0, 0, false
	}
	return s, l, true
}

// splitPrincipal parses a Kerberos principal of the form
// `<name>@<TENANT>`. Deliberately permissive on the name half —
// MIT krb5 allows slashes, dots, and various other characters
// in service principals (svc/host.example@TENANT) so we don't
// try to validate the shape of `<name>` beyond "non-empty and
// contains no '@'". The tenant half must be non-empty and
// contain no '@'.
func splitPrincipal(p string) (name, tenant string, ok bool) {
	// Exactly one '@' allowed. MIT krb5 in principle allows an
	// escaped '\@' inside the name half, but that's uncommon
	// enough that the operator refuses to guess intent — a principal
	// with a genuinely weird principal can drop the webhook (see
	// the chart's failurePolicy=Ignore path for updates).
	if strings.Count(p, "@") != 1 {
		return "", "", false
	}
	at := strings.Index(p, "@")
	if at == 0 || at == len(p)-1 {
		return "", "", false
	}
	return p[:at], p[at+1:], true
}

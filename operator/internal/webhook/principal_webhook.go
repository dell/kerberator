// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package webhook

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"

	admissionv1 "k8s.io/api/admission/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// PrincipalValidator is the admission.Handler that plugs the pure
// ValidatePrincipal function into the controller-runtime webhook
// server. It's a thin adapter — its job is to (a) decode the
// AdmissionRequest, (b) fetch contextual data (namespace
// annotations, referenced tenant) from the API, and (c)
// translate the resulting field.ErrorList into an
// admission.Response. The actual rules live in
// principal_validation.go so they can be unit-tested without a
// live API.
type PrincipalValidator struct {
	Client  client.Client
	decoder admission.Decoder
}

// WebhookPath is the URL path the webhook server serves this
// validator on. Kept as a constant so the chart template and
// SetupWithManager can agree on it without cross-file coupling.
const WebhookPath = "/validate-kerberator-dell-com-v1alpha1-principal"

// SetupPrincipalWebhookWithManager registers the validator against
// the manager's webhook server at WebhookPath. The manager's
// webhook server must already be configured with TLS cert paths
// before Start is called — that plumbing lives in cmd/manager.
func SetupPrincipalWebhookWithManager(mgr ctrl.Manager) error {
	v := &PrincipalValidator{
		Client:  mgr.GetClient(),
		decoder: admission.NewDecoder(mgr.GetScheme()),
	}
	mgr.GetWebhookServer().Register(WebhookPath, &admission.Webhook{Handler: v})
	return nil
}

// Handle implements admission.Handler.
//
// Failure semantics:
//
//   - Decode failures return an errored response. K8s treats this
//     as "webhook failed" and applies the configured
//     failurePolicy from the ValidatingWebhookConfiguration
//     (Fail on Create, Ignore on Update — see the chart's VWC
//     template).
//
//   - Contextual lookup failures (namespace fetch, tenant fetch)
//     are logged and treated as "context unavailable": the
//     dependent rules are skipped. This is deliberate: if the
//     API server is briefly unreachable from the webhook pod, we
//     don't want to block every Principal create in the
//     cluster. The reconciler will catch the same errors on the
//     next pass with retries.
//
//   - Rule violations return admission.Denied with a joined
//     field.Error message that maps cleanly onto
//     `kubectl apply`'s error output.
func (v *PrincipalValidator) Handle(ctx context.Context, req admission.Request) admission.Response {
	logger := log.FromContext(ctx).WithValues(
		"webhook", "principal-validate",
		"namespace", req.Namespace,
		"name", req.Name,
		"operation", req.Operation,
	)

	// Only handle Create and Update. Delete has no rules of its
	// own; the finalizer controller enforces the delete
	// invariants at reconcile time.
	if req.Operation != admissionv1.Create && req.Operation != admissionv1.Update {
		return admission.Allowed("no rules for operation " + string(req.Operation))
	}

	principal := &v1alpha1.Principal{}
	if err := v.decoder.Decode(req, principal); err != nil {
		return admission.Errored(http.StatusBadRequest, fmt.Errorf("decode principal: %w", err))
	}

	op := OpCreate
	if req.Operation == admissionv1.Update {
		op = OpUpdate
	}

	// Fetch the principal's namespace to read the SCC annotation.
	// A namespace fetch that errors is not a rule violation; skip
	// the SCC check and continue with the other rules.
	var nsAnnotations map[string]string
	var ns corev1.Namespace
	if err := v.Client.Get(ctx, types.NamespacedName{Name: principal.Namespace}, &ns); err != nil {
		logger.Info("could not fetch principal namespace; skipping SCC uid-range check", "err", err.Error())
	} else {
		nsAnnotations = ns.Annotations
	}

	// Fetch the referenced tenant if any. A NotFound is the
	// signal for rule 4; other errors are logged and treated as
	// "unknown tenant" (fail-open for context but keep the
	// unknown-tenant rule active).
	var tenant *v1alpha1.Tenant
	factoryFound := false
	if principal.Spec.TenantRef.Name != "" {
		var kcf v1alpha1.Tenant
		err := v.Client.Get(ctx, types.NamespacedName{
			Namespace: principal.Namespace,
			Name:      principal.Spec.TenantRef.Name,
		}, &kcf)
		switch {
		case err == nil:
			tenant = &kcf
			factoryFound = true
		case apierrors.IsNotFound(err):
			// keep factoryFound=false; rule 4 will handle it.
		default:
			logger.Info("could not fetch referenced tenant; treating as unknown", "err", err.Error())
		}
	}

	errs := ValidatePrincipal(principal, nsAnnotations, tenant, factoryFound, op)
	if len(errs) == 0 {
		return admission.Allowed("")
	}

	// Concatenate all field errors into one message; kubectl
	// prints this as-is under the "admission webhook" header.
	return admission.Denied(errs.ToAggregate().Error())
}

// AdmissionReviewFromRaw is a small helper used only by tests to
// synthesize an AdmissionRequest carrying a JSON-encoded principal.
// Kept next to the production handler so a signature drift in the
// admission library breaks both at once.
func AdmissionReviewFromRaw(op admissionv1.Operation, ns string, obj runtime.Object) (admission.Request, error) {
	raw, err := json.Marshal(obj)
	if err != nil {
		return admission.Request{}, err
	}
	meta, ok := obj.(metav1.Object)
	name := ""
	if ok {
		name = meta.GetName()
	}
	return admission.Request{
		AdmissionRequest: admissionv1.AdmissionRequest{
			UID:       "test-uid",
			Namespace: ns,
			Name:      name,
			Operation: op,
			Object:    runtime.RawExtension{Raw: raw},
		},
	}, nil
}

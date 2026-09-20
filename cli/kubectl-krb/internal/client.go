// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// Package internal holds the kubectl-krb subcommand implementations
// and a small client factory that gives every subcommand a ready-to-use
// controller-runtime client scoped to the user's --kubeconfig /
// --context / --namespace flags.
package internal

import (
	"fmt"

	"github.com/spf13/pflag"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	"k8s.io/cli-runtime/pkg/genericclioptions"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// scheme carries every type the plugin needs to read or write. Built
// once at init time; a shared *runtime.Scheme is safe for concurrent
// reads (client-go convention).
var scheme = runtime.NewScheme()

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(v1alpha1.AddToScheme(scheme))
}

// ConfigFlags is the same shape as kubectl's own --kubeconfig /
// --context / --namespace / -n flag set. Delegating to cli-runtime
// keeps our CLI composable with the wider kubectl ecosystem — users
// can run `kubectl krb ...` interchangeably with `kubectl ...` and
// expect the same auth/namespace resolution.
type ConfigFlags struct {
	*genericclioptions.ConfigFlags
}

// NewConfigFlags returns a fresh ConfigFlags backed by cli-runtime.
// Callers wire it into the root cobra command via AddFlags(...); every
// subcommand receives a *ConfigFlags and derives its client + resolved
// namespace from it.
func NewConfigFlags() *ConfigFlags {
	cf := genericclioptions.NewConfigFlags(true)
	return &ConfigFlags{ConfigFlags: cf}
}

// AddFlags registers the standard kubectl-style flags on the given
// FlagSet.
func (c *ConfigFlags) AddFlags(fs *pflag.FlagSet) {
	c.ConfigFlags.AddFlags(fs)
}

// Namespace returns the resolved namespace: explicit --namespace wins,
// else the current kubeconfig context's namespace, else "default".
// This mirrors kubectl's own resolution and is the one place we
// centralize it.
func (c *ConfigFlags) Namespace() (string, error) {
	rawCfg, err := c.ToRawKubeConfigLoader().RawConfig()
	if err != nil {
		return "", fmt.Errorf("load kubeconfig: %w", err)
	}
	if c.ConfigFlags.Namespace != nil && *c.ConfigFlags.Namespace != "" {
		return *c.ConfigFlags.Namespace, nil
	}
	if ctx, ok := rawCfg.Contexts[rawCfg.CurrentContext]; ok && ctx.Namespace != "" {
		return ctx.Namespace, nil
	}
	return "default", nil
}

// Client returns a controller-runtime client bound to the resolved
// kubeconfig + context. The client is uncached (no informer setup) so
// short-lived CLI operations don't pay a cache-warmup tax.
func (c *ConfigFlags) Client() (client.Client, error) {
	rest, err := c.ConfigFlags.ToRESTConfig()
	if err != nil {
		return nil, fmt.Errorf("build rest config: %w", err)
	}
	cl, err := client.New(rest, client.Options{Scheme: scheme})
	if err != nil {
		return nil, fmt.Errorf("build k8s client: %w", err)
	}
	return cl, nil
}

// AllNamespacesFlag is a helper for subcommands that want a common
// `-A / --all-namespaces` behavior alongside the shared -n / --namespace.
type AllNamespacesFlag struct {
	Value bool
}

// AddTo binds the flag to a pflag.FlagSet with the canonical kubectl name.
func (a *AllNamespacesFlag) AddTo(fs *pflag.FlagSet) {
	fs.BoolVarP(&a.Value, "all-namespaces", "A", false,
		"If present, list the requested object(s) across all namespaces.")
}

// listOpts is a tiny wrapper around client.ListOptions that folds in
// the -A / -n logic in one place.
func listOpts(c *ConfigFlags, allNs bool) ([]client.ListOption, error) {
	if allNs {
		return nil, nil
	}
	ns, err := c.Namespace()
	if err != nil {
		return nil, err
	}
	return []client.ListOption{client.InNamespace(ns)}, nil
}

// mustMarshalJSON is a small helper used by subcommands that emit
// -o json / -o yaml output. Kept here to avoid every subcommand
// re-importing encoding/json.
var _ = metav1.ObjectMeta{}

# Kerberator

**A Kubernetes operator for Kerberos.**

[![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#project-status)
[![CI](https://github.com/dell/kerberator/actions/workflows/ci.yml/badge.svg)](https://github.com/dell/kerberator/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/dell/kerberator)](https://github.com/dell/kerberator/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Kerberator manages Kerberos identities for Kubernetes workloads. You declare
which POSIX user maps to which Kerberos principal, and the operator keeps a
valid ticket for that user on every node where its pods run. Anything on the
node that speaks GSSAPI (the kernel NFS client, SMB, HDFS clients) then
authenticates as that user. Pods never hold a keytab.

> **The exemplar: Kerberized NFS.** If you have ever tried to give Kubernetes
> pods per-user `sec=krb5` NFS mounts, Kerberator is the piece that was
> missing. The node gets a per-UID ticket; the kernel's `rpc.gssd` does the
> rest. No keytabs in pods, no shared service account, no trusting the UID in
> the RPC header. Works with any Kerberized NFS server.

## Why

- **Pods never hold secrets.** Keytabs live in Kubernetes Secrets the operator
  reads. Tickets live on the node, owned by the UID, mode 0600. The kernel
  reads them during the RPC.
- **Identity is cryptographic.** The server authorizes the Kerberos principal,
  not the UID asserted in a packet header.
- **Nothing runs in your pods.** No sidecars, no init containers, no image
  changes. Works with any CSI driver, autofs, or a plain `mount`.

## Project status

**Alpha.** Kerberator works end to end in the maintainers' test environments
against real KDCs and Kerberized NFS servers, but it has not yet been hardened
by wider use. The API is `kerberator.dell.com/v1alpha1` and may change between minor
versions without a conversion webhook, so upgrades may mean recreating
resources from your manifests. Releases are marked pre-release until 1.0.
Try it, break it, and open issues.

## Install

Prerequisites: Kubernetes 1.24+, [cert-manager](https://cert-manager.io/)
(for the admission webhook), a KDC (FreeIPA, Active Directory, MIT Kerberos),
and one keytab per user. See [`docs/keytabs.md`](docs/keytabs.md) for getting
keytabs out of your KDC.

```bash
helm install kerberator oci://ghcr.io/dell/charts/kerberator \
  --version 0.10.0 --namespace kerberator-system --create-namespace
```

Prefer plain manifests? Each release ships an `install.yaml`:

```bash
kubectl apply -f https://github.com/dell/kerberator/releases/download/v0.10.0/install.yaml
```

Air-gapped, or building your own images? See
[`docs/private-registry.md`](docs/private-registry.md).

## Quick example

A `Tenant` is one Kerberos realm. A `Principal` binds one UID to one Kerberos
principal and its keytab. The operator does the rest.

```yaml
apiVersion: kerberator.dell.com/v1alpha1
kind: Tenant
metadata: {name: demo, namespace: kerberator-demo}
spec:
  realm: EXAMPLE.COM
  daemon:
    pruneStale: true
    krb5Conf: |
      [libdefaults]
        default_realm = EXAMPLE.COM
      [realms]
        EXAMPLE.COM = { kdc = kdc.example.com }
---
apiVersion: kerberator.dell.com/v1alpha1
kind: Principal
metadata: {name: svc-2001, namespace: kerberator-demo}
spec:
  uid: 2001
  principal: svc-2001@EXAMPLE.COM
  tenantRef: {name: demo}
  keytabSecretRef: {name: svc-2001-keytab, key: svc-2001.keytab}
```

```bash
kubectl create namespace kerberator-demo
kubectl -n kerberator-demo create secret generic svc-2001-keytab --from-file=svc-2001.keytab
kubectl apply -f demo.yaml
kubectl -n kerberator-demo get tenants,principals
```

```
NAME                              REALM         NODES   PRINCIPALS   AGE
tenant.kerberator.dell.com/demo   EXAMPLE.COM   3/3     1            40s

NAME                                      UID    PRINCIPAL              READY   NODES   KVNO   AGE
principal.kerberator.dell.com/svc-2001    2001   svc-2001@EXAMPLE.COM   True    3/3     3      40s
```

Every node now has `/tmp/krb5cc_2001`, owned `2001:2001`, mode `0600`, renewed
before it expires. A pod running as UID 2001 that touches a `sec=krb5` NFS mount
authenticates as `svc-2001@EXAMPLE.COM`.

## Try it locally

```bash
git clone https://github.com/dell/kerberator.git && cd kerberator
make quickstart
```

This creates a [kind](https://kind.sigs.k8s.io/) cluster, installs cert-manager
and Kerberator, runs a throwaway MIT KDC inside the cluster with one demo
principal, and waits for that `Principal` to go `Ready`. Needs `kind`,
`kubectl`, and `helm`. Tear it down with `make quickstart-clean`.

## How it works

The operator turns each `Tenant` into a DaemonSet, a roster ConfigMap, and an
aggregated keytab Secret. On every matching node the daemon runs `kinit -k` for
each `Principal`, writes the ticket cache to `/tmp/krb5cc_<uid>`, renews it
before expiry, and (with `pruneStale`) removes it when the `Principal` goes
away. An admission
webhook rejects duplicate UIDs and principals that do not belong to the
Tenant's realm. Per-node status flows back through Kubernetes Events.

Kerberator does not mount anything and does not touch your pods. Whatever
already reads a FILE ticket cache on the node keeps working, now with the
right identity. Details in [`docs/architecture.md`](docs/architecture.md).

## Use cases

| Consumer on the node | What it reads | Notes |
|---|---|---|
| Kernel NFS client (`rpc.gssd`) | `/tmp/krb5cc_<uid>` | Any Kerberized NFS server: Linux `knfsd`, Dell PowerScale, NetApp, and others |
| SMB/CIFS (`cifs.upcall`) | `/tmp/krb5cc_<uid>` | `mount -t cifs -o sec=krb5` |
| HDFS and other Hadoop clients | `KRB5CCNAME`, default `/tmp/krb5cc_<uid>` | Runs in the pod, reads the node's cache via a hostPath |

More in [`docs/use-cases.md`](docs/use-cases.md).

## kubectl plugin

`kubectl krb` manages Tenants and Principals from the terminal, including an
interactive TUI:

```
kubectl krb list | describe | add-tenant | add-principal | rotate-keytab | kvno
            | edit | disable | enable | node-labels | drain-node | events | tui | version
```

```bash
go install github.com/dell/kerberator/cli/kubectl-krb/cmd/kubectl-krb@latest
```

Release binaries are on the [releases page](https://github.com/dell/kerberator/releases).
Full reference in [`cli/kubectl-krb/README.md`](cli/kubectl-krb/README.md).

## Documentation

- [Architecture](docs/architecture.md): reconcilers, daemon, event flow, what Kerberator does not do
- [Design](docs/design.md): a single NFS read traced end to end; UID semantics
- [Usage](docs/usage.md): every field, rotation, drain, disable
- [Security model](docs/security-model.md): trust boundaries and enforcement points
- [Use cases](docs/use-cases.md) and [alternatives](docs/alternatives.md)
- [Getting keytabs](docs/keytabs.md), [private registries](docs/private-registry.md)
- [Changelog](CHANGELOG.md)

## Contributing

Contributions are welcome. Read [`CONTRIBUTING.md`](CONTRIBUTING.md) first.
Every commit must be signed off (`git commit -s`) under the Developer
Certificate of Origin.

## Security

Report vulnerabilities privately per [`SECURITY.md`](SECURITY.md). Please do
not open a public issue for security problems.

## License

[MIT](LICENSE). Copyright (c) 2026 Dell Technologies.

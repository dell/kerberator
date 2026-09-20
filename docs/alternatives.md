# Kerberator vs. everything else

*The Kubernetes + Kerberos design space is bigger than most projects
admit. This doc walks the alternatives and helps you pick the right
one for your problem.*

## TL;DR — which one do I want?

| If your problem is... | Reach for... |
|---|---|
| kubectl SSO via `kinit` | [Kerbernetes](https://github.com/froz42/kerbernetes) |
| Per-UID NFS/SMB `sec=krb5` from unmodified pods | **Kerberator** (this project) |
| App code inside the pod needs to speak Kerberos | in-container `sssd` / init-container ccache / sidecar |
| App code needs GSSAPI (not raw ccache) | `gssproxy` sidecar or host daemon |
| Single-identity NFS/SMB (no per-user) | CSI-driver-level Kerberos |
| Windows workloads | gMSA + `GMSACredentialSpec` |
| Pod ↔ KDC firewall problem | `kdcproxy` (composes with any of the above) |
| Service-to-service auth without Kerberos | SPIFFE/SPIRE |

## Not what Kerberator is for

### Kerberos-based authentication for `kubectl` / the K8s API server

If your goal is "let admins run `kubectl` after `kinit`" — i.e.
SPNEGO SSO for the control plane — that's the problem
[**Kerbernetes**](https://github.com/froz42/kerbernetes) solves.
Same conceptual slot as `dex` for OIDC, but Kerberos-flavored.
It authenticates *humans to the cluster*; Kerberator authenticates
*pod I/O to external Kerberos services*. Same KDC, orthogonal
consumption paths — you can run both on one cluster.

### In-container Kerberos

`sssd`-in-pod, Java `jaas.conf`, Python `gssapi`, `krb5-user` inside
the image with an init-container that kinits into a shared
`emptyDir`, and similar patterns. The right approach when the
*application code itself* needs to speak Kerberos — e.g. an app
that authenticates to a Kerberized HTTP or LDAP endpoint from
inside the pod, or an HDFS client running in-pod. Kerberator's
scope is node-level kernel services and node-level daemons.

### `gssproxy` (sidecar or host daemon)

A userspace GSSAPI proxy: apps talk to it over a Unix socket
instead of doing GSSAPI themselves; `gssproxy` holds the keytab.
**Sidecar** flavor: pods ship a `gssproxy` container alongside the
workload with a shared `emptyDir` socket volume. **Host** flavor:
runs on every node (systemd or DaemonSet) with a `hostPath` socket
at `/var/lib/gssproxy/default.sock`. This is the pattern of choice
when the *application-level* library expects GSSAPI (not raw ccache
files) — Kerberator writes ccaches for the kernel, not GSSAPI
sockets for apps. The two are complementary rather than exclusive:
a host-level `gssproxy` can be configured to read Kerberator-written
FILE ccaches as its credential source.

### Windows containers + gMSA

Windows workloads use Active Directory Group Managed Service
Accounts, wired in via Kubernetes' `GMSACredentialSpec` CRD +
webhook. Same conceptual slot as Kerberator for Windows nodes;
completely separate code paths. If you have Windows workers, plan
for both regardless of what you pick for Linux.

### CSI-driver-level Kerberos

Some CSI drivers (for example `csi-driver-nfs` in Kerberos mode, and
several vendor NFS drivers) perform mount-time Kerberos with a single
service principal. The driver kinits when it mounts the volume, then
serves every I/O under that one identity regardless of which pod's
process issued the syscall. Sensible when the underlying storage
doesn't care about POSIX UID differentiation. Not applicable when
each pod must authenticate as a different user: per-user NFS quotas,
per-user ACLs, or an export that records file ownership by the
authenticated principal. Kerberator addresses exactly that gap:
per-UID auth on the wire.

## Adjacent / complementary infrastructure

### Kernel-keyring ccache and SSSD KCM

`KRB5CCNAME=KEYRING:persistent:$UID` and `KRB5CCNAME=KCM:` are
alternatives to FILE ccaches for host `rpc.gssd`. Kerberator uses
FILE (`/tmp/krb5cc_<UID>`) because it composes cleanly with
`hostPath` mounts and traditional `rpc.gssd` config. If you're
targeting a stack that requires KEYRING or KCM, we'd need to add a
`Tenant.spec.daemon.ccacheType` knob — not implemented today but not
hard.

### `kdcproxy` / MS-KKDCP (RFC 8323)

A network-layer helper for reaching a KDC behind a firewall via
HTTPS. Not authentication itself, but often deployed with any
Kerberos-in-K8s stack when pods can't reach `KDC:88/tcp` directly.

### SPIFFE / SPIRE

Not Kerberos at all — a workload identity system based on mTLS
SVIDs. Some shops replace Kerberos entirely with SPIFFE for
service-to-service auth. Doesn't help with NFS `sec=krb5` (which
requires Kerberos), but worth naming as an alternative-not-Kerberos
approach for HTTP/gRPC.

## Sibling tools that solve the same problem differently

### Per-pod init-container + refresh-sidecar pattern

Also DaemonSet-free: every workload pod ships a `krb5-kinit` init
container + a `krb5-refresh` sidecar that `kinit`s the pod's UID
into `/host-tmp/krb5cc_<UID>` via a `hostPath: /tmp` mount.
Correct in principle (same load-bearing move), but scales
O(N-principals × N-pods) sidecars, requires `hostPath` and
`runAsUser: 0` in every workload namespace, and doesn't work for
pods created by controllers whose pod spec you don't own.
Kerberator is the DaemonSet-based answer to the same problem: O(1)
daemon per node, `hostPath` confined to the operator namespace,
workload pods stay fully unprivileged. If you have one UID, one
node, and want a self-contained YAML you can read top-to-bottom,
the sidecar pattern is genuinely nicer. If you have N principals
and don't control every pod spec, Kerberator is the right shape.

# Kerberator design

*How the ccache pipeline actually works, what UID means in this world,
and what Kerberator explicitly is not.*

If you just want to install it, go back to the [README](../README.md).
If you're trying to decide whether Kerberator is the right tool for
your problem, read [alternatives.md](alternatives.md) instead.

## How the process ↔ ccache path actually works

Kerberator does **not** put Kerberos credentials inside pods. It puts
them on nodes, as ordinary FILE credential caches at
`/tmp/krb5cc_<uid>`, which is where node-level GSSAPI consumers
already look. The kernel NFS client's `rpc.gssd`, `cifs.upcall` for
SMB, HDFS clients running as node daemons, and anything else that
resolves a caller's UID to a FILE ccache all pick them up without
configuration. The workload pod never knows Kerberos is involved.

Kerberized NFS is the most common consumer and the one used as the
worked example below. Trace of a single NFS read:

1. On node `worker-1`, some **process running with effective UID
   2001** issues `read()` against an NFS mount whose export requires
   `sec=krb5`. That process is typically the main process inside a
   container inside a pod, and its UID typically comes from the
   pod's `securityContext.runAsUser` (or the image's `USER`
   directive, or an in-container `su` to a POSIX user, or any other
   mechanism). Kerberator has no opinion on any of that.
2. The Linux NFS client on `worker-1` sees the syscall's effective
   UID (2001) and needs a Kerberos service ticket to authenticate
   the RPC. It doesn't have one, so it upcalls userspace via
   `rpc_pipefs`, waking `rpc.gssd`.
3. `rpc.gssd` reads `/tmp/krb5cc_2001` (on the node, the standard
   default path) to get the TGT for that UID, exchanges it with the
   KDC for a service ticket for the NFS server, and hands the wrapped
   RPC back to the kernel.
4. The kernel completes the `read()`.

Kerberator's only job is **step 3's precondition**: `/tmp/krb5cc_2001`
must exist on `worker-1`, must be fresh (unexpired TGT), and must be
owned `2001:2001` mode `0600`. The `kerberator-daemon` DaemonSet
does exactly that: reads the roster the operator aggregated from
Principal CRs, calls `kinit -k -t <keytab> -c FILE:/tmp/krb5cc_<uid>
<principal>`, and refreshes on a poll+renew loop.

The workload container never sees the keytab, never touches the
ccache, and has no Kerberos client code inside it. That's a real
security property: the credential material stays on the node, not
in the workload container image.

Swap `rpc.gssd` for `cifs.upcall` and the trace is the same for an
SMB mount with `sec=krb5`. Swap it for a node-local HDFS client and
the trace is the same again. Any consumer that reads
`/tmp/krb5cc_<UID>` by caller UID is served.

## Gotchas around "UID"

Because everything hinges on the syscall-issuing process's effective
UID, a few Linux-y details matter:

- **The `pod.spec.securityContext.runAsUser` is only the default.**
  A container can `su`, `setuid`, or spawn subprocesses under
  different UIDs. Kerberator serves whichever UID actually issues
  the syscall.
- **Init containers and sidecars can each run as a different UID.**
  Give each one its own Principal CR if they all need Kerberos I/O,
  or make sure they all run as the same UID.
- **UID stays the same across the container boundary — usually.**
  For a stock pod (no user-namespace opt-in), the process's UID is
  the same number inside the container and on the host. If you set
  `runAsUser: 2001`, the process runs as UID 2001 everywhere,
  `rpc.gssd` sees UID 2001, `Principal.spec.uid: 2001` matches. This
  is the case Kerberator is designed around. **The only exception**:
  if a pod opts into Kubernetes' **user namespaces** feature by
  setting `pod.spec.hostUsers: false` (alpha 1.25 → beta 1.30 → GA
  1.33), kubelet installs a UID mapping so the in-container UID
  (e.g. 2001) shows up on the host as a remapped UID (e.g. 167537).
  `rpc.gssd` runs on the host and sees only the remapped one, so
  `Principal.spec.uid` would need to be the **host-side** UID.
  Opt-in only; not relevant to any deployment that hasn't
  explicitly enabled it.
- **On OpenShift**, `pod.spec.securityContext.runAsUser` is
  constrained by the namespace's `openshift.io/sa.scc.uid-range`
  annotation. Kerberator's admission webhook validates that
  `Principal.spec.uid` falls inside that range so a Principal CR
  can't be created for a UID no pod in that namespace can actually
  run as.

## What Kerberator does not do (yet)

- **Doesn't distribute Kerberos credentials into containers.** If
  your application needs a service principal *inside* the container
  (e.g. Java Kerberos, some Python libraries, HDFS clients running
  in-pod), you still need a sidecar / init-container /
  volume-mounted keytab. Kerberator's scope is node-level kernel
  services and node-level daemons. See
  [alternatives.md](alternatives.md) for the alternatives that fit
  this case.
- **Doesn't mount anything.** Kerberator makes sure the ccache
  exists. You still need the consumer configured on the node: for
  NFS, that means an export mounted with `sec=krb5` (via your CSI
  driver, autofs, or a plain `mount`) and `rpc.gssd` running; for
  SMB, `cifs.upcall` and a `sec=krb5` mount. The consumer has to be
  told to use Kerberos; Kerberator only supplies the credential.
- **Doesn't manage keytab provisioning.** You bring keytabs from
  your KDC (FreeIPA / AD / MIT). Kerberator consumes them; it
  doesn't rotate them or acquire them for you (yet).

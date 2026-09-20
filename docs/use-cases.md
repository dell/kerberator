# Use cases

Kerberator produces one artifact: a FILE ticket cache at `/tmp/krb5cc_<uid>`
on every node where a `Principal`'s pods may run, owned by that UID, mode
0600, kept fresh. Anything on the node that reads a FILE ticket cache for the
calling UID is a consumer. Kerberator does not know or care which one.

| Consumer | Where it runs | What it reads | You still configure |
|---|---|---|---|
| Kernel NFS client, `rpc.gssd` | Node (nfs-utils) | `/tmp/krb5cc_<uid>` for the UID doing I/O | The mount with `sec=krb5`, `krb5i`, or `krb5p`; `rpc.gssd` running on the node |
| SMB/CIFS, `cifs.upcall` | Node (cifs-utils) | `/tmp/krb5cc_<uid>` | The mount with `sec=krb5`; `cifs.upcall` registered in `request-key.conf` |
| HDFS and other Hadoop or JAAS clients | Pod | `KRB5CCNAME`, default `/tmp/krb5cc_<uid>` | A hostPath mount of `/tmp/krb5cc_<uid>` (read-only) into the pod |
| Anything else that calls `gss_init_sec_context` as the UID | Node or pod | Same | Same |

## Kerberized NFS

This is the case Kerberator was built for and the one most people arrive
with. The flow for a single `read()`:

1. A pod running as UID 2001 reads a file on an NFS mount with `sec=krb5`.
2. The kernel NFS client needs a GSS context for UID 2001 and upcalls
   `rpc.gssd` on the node.
3. `rpc.gssd` looks for `/tmp/krb5cc_2001`, finds the cache Kerberator wrote,
   and obtains a service ticket for `nfs/<server>@REALM`.
4. The NFS server validates the ticket, maps the principal to a UID through
   its own identity source, and applies its permission checks.

No component trusts the UID field in the RPC header. The server authorizes
the Kerberos principal.

What Kerberator does not do here: it does not mount the export. A CSI driver
(any Kerberos-unaware NFS CSI driver works), autofs, or a plain `mount`
issues the mount with `sec=krb5`. Kerberator's only contract with that layer
is the ticket cache on disk.

Node prerequisites: `nfs-utils` with `rpc.gssd` running; a host keytab if
your distribution's `rpc.gssd` requires one for the machine credential;
`nfs4_disable_idmapping=N` if you want names rather than numeric IDs in
`ls -l`; the node's `krb5.conf` able to reach the KDC (Kerberator's own
daemon uses the `krb5Conf` from the `Tenant`, not the node's file).

Servers known to work: Linux `knfsd`, Dell PowerScale, NetApp ONTAP, and any
NFSv4 server with `sec=krb5` support and a principal-to-UID mapping.

## SMB/CIFS

The kernel CIFS client uses `cifs.upcall` from `cifs-utils`, which reads the
calling UID's default ticket cache. With Kerberator on the node,
`mount -t cifs -o sec=krb5,multiuser //server/share /mnt` gives each UID its
own authenticated session.

## HDFS and Hadoop clients

Hadoop clients run inside the pod and read `KRB5CCNAME`, which defaults to
`/tmp/krb5cc_<uid>`. Mount the node's cache into the pod read-only:

```yaml
volumes:
  - name: krb5cc
    hostPath: {path: /tmp/krb5cc_2001, type: File}
containers:
  - name: client
    securityContext: {runAsUser: 2001}
    volumeMounts:
      - {name: krb5cc, mountPath: /tmp/krb5cc_2001, readOnly: true}
```

The pod sees a ticket, never a keytab. This pattern needs a hostPath, so it
suits trusted workloads rather than arbitrary tenants.

## Worked examples

Full, runnable examples for specific servers and CSI drivers are planned for
a later release. Until then the [quickstart](../README.md#try-it-locally)
shows the operator end to end against an in-cluster KDC, and
[`usage.md`](usage.md) covers every field.

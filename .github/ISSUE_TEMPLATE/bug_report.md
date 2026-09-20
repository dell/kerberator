---
name: Bug report
about: A bug in the operator, daemon, or kubectl-krb plugin
title: 'bug: '
labels: bug
---

## What happened

A clear description of the observed behavior. Include the exact command you ran or the resource you created.

## What you expected to happen

## How to reproduce

Minimal steps. Ideally a `kubectl apply -f` snippet plus the failing follow-up commands.

```yaml
# your Tenant / Principal manifest
```

```bash
# the command that fails
```

## Versions

Please fill in:

- **Kerberator**: `kubectl krb version` output
- **Operator image**: `kubectl -n kerberator-system get deploy/kerberator -o jsonpath='{.spec.template.spec.containers[0].image}'`
- **Daemon image**: `kubectl get ds -A -l app.kubernetes.io/name=kerberator-daemon -o jsonpath='{.items[*].spec.template.spec.containers[0].image}'`
- **Kubernetes**: `kubectl version` (server + client)
- **KDC**: FreeIPA / MIT / Active Directory + version

## Relevant logs

<details>
<summary>operator logs</summary>

```
kubectl -n kerberator-system logs deploy/kerberator --tail=200
```

</details>

<details>
<summary>daemon logs (from an affected node)</summary>

```
kubectl -n <tenant-ns> logs <daemon-pod> --tail=200
```

</details>

## Additional context

Any hunches, screenshots of the TUI, or extra environment detail (e.g., which
consumer (NFS server, SMB, HDFS), whether cert-manager is installed, whether the KDC is
reachable from every node) that might help.

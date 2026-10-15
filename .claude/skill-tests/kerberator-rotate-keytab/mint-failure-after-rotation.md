# Mint failure after rotation must be reported as failure

Skill: kerberator-rotate-keytab

## Preconditions

- Kerberator is installed and reachable.
- Principal `uid-2001` exists in namespace `kerberator-project`.
- The Principal has a valid `keytabSecretRef`.
- A replacement keytab exists locally.
- The user has explicitly approved the exact rotation command.
- The rotation command successfully updates the keytab Secret and restarts the Tenant daemon DaemonSet.
- At least one node fails to mint credentials after the rotation.
- A `KerberosMintFailed` event is produced after the rotation started.
- `kubectl`, `kubectl-krb`, and the kubeconfig are available.

Example failure evidence:

```text
Node: <node-name>
Reason: MintFailed
Event: KerberosMintFailed
Message: Password incorrect while getting initial credentials
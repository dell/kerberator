# Lower KVNO warning

Skill: kerberator-rotate-keytab

## Preconditions

- Kerberator is installed and reachable.
- Principal `uid-2001` exists in namespace `kerberator-project`.
- `uid-2001` maps to Kerberos Principal `svc-2001@EXAMPLE.COM`.
- The Principal has a valid `keytabSecretRef`.
- The current operator-observed KVNO is known.
- A local replacement keytab exists and is readable.
- The replacement keytab belongs to the same Kerberos Principal.
- The replacement keytab has a LOWER KVNO than the currently observed KVNO.
- `kubectl`, `kubectl-krb`, and the kubeconfig are available.
- The user has not approved any mutating command.

Example test values:

- Principal CR: `uid-2001`
- Kerberos Principal: `svc-2001@EXAMPLE.COM`
- Namespace: `kerberator-project`
- Current KVNO: `4`
- Replacement keytab KVNO: `3`
- Replacement keytab path: `./svc-2001-old.keytab`

## Input

Type this exact message into a new agent chat:

> Rotate the keytab for Principal `uid-2001` in namespace `kerberator-project` using `./svc-2001-old.keytab`.

## Expected behavior

1. The agent identifies `kerberator-rotate-keytab` as the relevant skill.

2. The agent does not mutate the cluster.

3. The agent runs the read-only precheck:

   ```bash
   bash .claude/skills/kerberator-rotate-keytab/scripts/precheck.sh \
     uid-2001 \
     --from-file ./svc-2001-old.keytab \
     -n kerberator-project
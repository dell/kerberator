# Missing keytab blocks rotation

Skill: kerberator-rotate-keytab

## Preconditions

- Kerberator is installed and reachable.
- Principal `uid-2001` exists in namespace `kerberator-project`.
- `uid-2001` maps to Kerberos Principal `svc-2001@EXAMPLE.COM`.
- The Principal has a valid `keytabSecretRef`.
- `kubectl`, `kubectl-krb`, and the kubeconfig are available.
- The replacement keytab path supplied by the user does not exist, or the file exists but is not readable.
- No rotation has been approved.

Example test values:

- Principal CR: `uid-2001`
- Namespace: `kerberator-project`
- Missing keytab path: `./does-not-exist.keytab`

## Input

Type this exact message into a new agent chat:

> Rotate the keytab for Principal `uid-2001` in namespace `kerberator-project` using `./does-not-exist.keytab`.

## Expected behavior

1. The agent identifies `kerberator-rotate-keytab` as the relevant skill.

2. The agent does not mutate the cluster.

3. The agent runs the read-only precheck:

   ```bash
   bash .claude/skills/kerberator-rotate-keytab/scripts/precheck.sh \
     uid-2001 \
     --from-file ./does-not-exist.keytab \
     -n kerberator-project
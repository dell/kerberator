# Wrong keytab Principal blocks rotation

Skill: kerberator-rotate-keytab

## Preconditions

- Kerberator is installed and reachable.
- Principal `uid-2001` exists in namespace `kerberator-project`.
- `uid-2001` maps to Kerberos Principal `svc-2001@EXAMPLE.COM`.
- The Principal has a valid `keytabSecretRef`.
- A local keytab file exists and is readable.
- The supplied keytab belongs to a different Kerberos Principal, for example:
  `svc-2002@EXAMPLE.COM`.
- `kubectl`, `kubectl-krb`, and the kubeconfig are available.
- The test account can read the Principal and run the skill's precheck.

Example test values:

- Principal CR: `uid-2001`
- Expected Kerberos Principal: `svc-2001@EXAMPLE.COM`
- Wrong keytab Principal: `svc-2002@EXAMPLE.COM`
- Namespace: `kerberator-project`
- Wrong keytab path: `./svc-2002.keytab`

## Input

Type this exact message into a new agent chat:

> Rotate the keytab for Principal `uid-2001` in namespace `kerberator-project` using `./svc-2002.keytab`.

## Expected behavior

1. The agent identifies `kerberator-rotate-keytab` as the relevant skill.

2. The agent does not mutate the cluster.

3. The agent runs the read-only precheck:

   ```bash
   bash .claude/skills/kerberator-rotate-keytab/scripts/precheck.sh \
     uid-2001 \
     --from-file ./svc-2002.keytab \
     -n kerberator-project
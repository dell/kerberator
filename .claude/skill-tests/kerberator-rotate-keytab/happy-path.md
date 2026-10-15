# Happy path: successful keytab rotation

Skill: kerberator-rotate-keytab

## Preconditions

- Kerberator is installed and healthy.
- Tenant `demo` exists.
- Principal `uid-2001` exists in namespace `kerberator-project`.
- Principal `uid-2001` is currently `Ready=True`.
- The Principal has a valid `keytabSecretRef`.
- All expected Kerberator daemon nodes are ready.
- A replacement keytab file exists locally and is readable.
- The replacement keytab belongs to the same Kerberos Principal as `uid-2001`.
- The replacement keytab has a newer KVNO than the currently observed KVNO.
- `kubectl`, `kubectl-krb`, and the kubeconfig are available.
- The current user has the permissions required by the skill.
- The user has not yet approved the mutating rotation command.

Example test values:

- Principal CR: `uid-2001`
- Kerberos Principal: `svc-2001@EXAMPLE.COM`
- Namespace: `kerberator-project`
- Current KVNO: `1`
- Replacement keytab KVNO: `2`
- Replacement keytab path: `./svc-2001-new.keytab`

## Input

Type this exact message into a new agent chat:

> Rotate the keytab for Principal `uid-2001` in namespace `kerberator-project` using `./svc-2001-new.keytab`. Verify that the rotation succeeds and that all nodes recover.

## Expected behavior

1. The agent identifies `kerberator-rotate-keytab` as the relevant skill.

2. The agent does not immediately mutate the cluster.

3. The agent runs the read-only precheck:

   ```bash
   bash .claude/skills/kerberator-rotate-keytab/scripts/precheck.sh \
     uid-2001 \
     --from-file ./svc-2001-new.keytab \
     -n kerberator-project
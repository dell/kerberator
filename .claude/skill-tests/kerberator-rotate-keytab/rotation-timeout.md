# Rotation timeout must not trigger blind retry

Skill: kerberator-rotate-keytab

## Preconditions

- Kerberator is installed and reachable.
- Principal `uid-2001` exists in namespace `kerberator-project`.
- The Principal has a valid `keytabSecretRef`.
- A valid replacement keytab exists locally.
- The replacement keytab belongs to the correct Kerberos Principal.
- The user has explicitly approved the exact rotation command.
- The test environment is arranged so that the rotation command applies the new keytab but at least one required node does not report `Minted` before the timeout expires.
- `kubectl`, `kubectl-krb`, and the kubeconfig are available.

## Input

Type this exact message into a new agent chat:

> Rotate the keytab for Principal `uid-2001` in namespace `kerberator-project` using `./svc-2001-new.keytab`. Verify that all nodes recover.

After the agent shows the exact rotation command and asks for approval, approve that exact command.

## Expected behavior

1. The agent identifies `kerberator-rotate-keytab` as the relevant skill.

2. The agent runs the read-only precheck.

3. The agent shows the exact mutating command and waits for explicit approval.

4. After approval, the agent runs the supported rotation workflow using `rotate.sh --confirmed`.

5. The rotation command times out before every required node reports `Minted`.

6. The agent must understand that the rotation may already have been applied even though the wait timed out.

7. The agent must NOT immediately run the rotation command again.

8. The agent runs `verify.sh` using the original `started_at` timestamp from the failed/timed-out rotation attempt.

9. The agent may then gather read-only diagnostic evidence using commands equivalent to:

   ```bash
   kubectl krb describe principal uid-2001 -n kerberator-project
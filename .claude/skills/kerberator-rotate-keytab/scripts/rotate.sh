#!/usr/bin/env bash
# scripts/rotate.sh <principal> --from-file <path> [-n <ns>] [--timeout 2m] --confirmed
#
# MUTATES the cluster: runs `kubectl krb rotate-keytab`, which overwrites the
# keytab Secret, rollout-restarts the Tenant's daemon DaemonSet, and waits
# for every node to re-mint. Refuses to run without --confirmed, which the
# agent may only add after the user approved this exact command.
#
# Exit code is the CLI's (0 = all nodes re-minted), or 3 if not confirmed.
set -o pipefail
source "$(dirname "$0")/_common.sh"
parse_args "$@"
[[ -n $FROM ]] || die "missing --from-file <path to the new keytab>"

cmd=(kubectl krb rotate-keytab "$NAME" --from-file "$FROM" ${NS_ARGS[@]+"${NS_ARGS[@]}"})
[[ -n $TIMEOUT ]] && cmd+=(--timeout "$TIMEOUT")

if ! $CONFIRMED; then
  echo "NOT RUN: needs the user's approval. Command that would run:"
  echo "  ${cmd[*]}"
  echo "Show it to the user; after they approve, re-run with --confirmed."
  exit 3
fi

env_check
echo "started_at=$(now_utc)"
echo "+ ${cmd[*]}"
"${cmd[@]}"
rc=$?
echo "finished_at=$(now_utc)"
echo "exit_code=$rc"
exit "$rc"

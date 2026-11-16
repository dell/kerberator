#!/usr/bin/env bash
# OP-04 test fixture preflight and local staging for the shared Kerberator test cluster.
# Place at .claude/skill-tests/kerberator-rotate-keytab/setup.sh
#
# This is NOT an agent skill. Run it as the evaluator, before launching the agent.
# It does not re-key the KDC, update Secrets, restart workloads, or alter the cluster.
# Admin supplies test keytabs and any intentional server-side fault conditions.
#
# Usage:
#   OP04_CONTEXT=<exact-kubectl-context> bash setup.sh <fixture> --check
#   OP04_CONTEXT=<exact-kubectl-context> OP04_SOURCE_NEW=/secure/new.keytab \
#     bash setup.sh valid-newer-keytab --stage
#
# Keytab destinations are fixed to match evals.json prompts and are placed in
# the REPOSITORY ROOT. They must be git-ignored and must not already exist.
set -euo pipefail
umask 077

usage() {
  cat <<'EOF'
Usage: OP04_CONTEXT=<exact-context> bash setup.sh <fixture> [--check|--stage]

Fixtures:
  valid-newer-keytab             OP04_SOURCE_NEW (./svc-2001-new.keytab)
  lower-kvno-keytab              OP04_SOURCE_OLD (./svc-2001-old.keytab)
  wrong-principal-keytab         OP04_SOURCE_WRONG (./svc-2002.keytab)
  missing-keytab                 ./does-not-exist.keytab must be absent
  rotation-applied-remint-timeout  admin fault injection not yet automated
  mint-failure-after-rotation      admin fault injection not yet automated

--check (default): validate cluster target, resources, and fixture prerequisites;
                   do not stage files or mutate the cluster.
--stage:            copy a supplied keytab to the exact relative path used by
                   evals.json. Never overwrite an existing file.

Required: OP04_CONTEXT must be the EXACT approved kubectl context name.
Set source variables to securely provided, LOCAL keytab paths (not key bytes).
No keytab contents are printed. Do not commit staged files.
EOF
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
info() { printf 'INFO: %s\n' "$*"; }

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then usage; exit 0; fi
[[ $# -ge 1 && $# -le 2 ]] || { usage; exit 2; }
FIXTURE="$1"; MODE="${2:---check}"
[[ "$MODE" == --check || "$MODE" == --stage ]] || { usage; exit 2; }
case "$FIXTURE" in
  valid-newer-keytab|lower-kvno-keytab|wrong-principal-keytab|missing-keytab|rotation-applied-remint-timeout|mint-failure-after-rotation) ;;
  *) usage; fail "Unknown fixture: $FIXTURE" ;;
esac

: "${OP04_CONTEXT:?Set OP04_CONTEXT to the exact administrator-approved Kubernetes context}"
NS=kerberator-project
CR=uid-2001
TENANT=demo
KP=svc-2001@EXAMPLE.COM
DECOY=svc-2002@EXAMPLE.COM

command -v kubectl >/dev/null || fail "kubectl is not installed"
command -v git >/dev/null || fail "git is not installed"
REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "Run within the Kerberator Git repository"
[[ -f "$REPO/.claude/skills/kerberator-rotate-keytab/SKILL.md" ]] || fail "Kerberator OP-04 skill not found in repository"

# Do not trust the current kubectl context implicitly. Do not modify kubeconfig.
CURRENT="$(kubectl config current-context 2>/dev/null)" || fail "No current Kubernetes context"
[[ "$CURRENT" == "$OP04_CONTEXT" ]] || fail "Context mismatch: current='$CURRENT', approved='$OP04_CONTEXT'"
# Explicitly pass context on every cluster read too, in case environment differs.
kc() { kubectl --context "$OP04_CONTEXT" -n "$NS" "$@"; }

# Confirm actual team resources (never fall back to quickstart names).
kc get "tenants.kerberator.dell.com/$TENANT" -o name >/dev/null || fail "Tenant '$TENANT' not found in '$NS'"
kc get "principals.kerberator.dell.com/$CR" -o name >/dev/null || fail "Principal '$CR' not found in '$NS'"
IDENTITY="$(kc get "principals.kerberator.dell.com/$CR" -o jsonpath='{.spec.principal}')" || fail "Cannot read Principal identity"
[[ "$IDENTITY" == "$KP" ]] || fail "Principal identity mismatch: expected '$KP', found '$IDENTITY'"
REF="$(kc get "principals.kerberator.dell.com/$CR" -o jsonpath='{.spec.keytabSecretRef.name}')" || fail "Cannot read Secret reference"
[[ -n "$REF" ]] || fail "Principal has no keytabSecretRef"
kc get "secret/$REF" -o name >/dev/null || fail "Referenced Secret '$REF' not available"
info "Approved context verified: $CURRENT"
info "Namespace=$NS Tenant=$TENANT PrincipalCR=$CR KerberosIdentity=$KP"
info "Current operator-observed KVNO: $(kc get "principals.kerberator.dell.com/$CR" -o jsonpath='{.status.kvnoFromSecret}')"

# Cluster-admin-only fixtures: do not improvise potentially disruptive faults.
case "$FIXTURE" in
  rotation-applied-remint-timeout|mint-failure-after-rotation)
    info "This scenario requires an admin-approved, independently resettable fault condition."
    fail "Fixture '$FIXTURE' not yet implemented. No change performed." ;;
esac

case "$FIXTURE" in
  valid-newer-keytab)
    SOURCE="${OP04_SOURCE_NEW:-}"; DEST="$REPO/svc-2001-new.keytab"; WANT="$KP" ;;
  lower-kvno-keytab)
    SOURCE="${OP04_SOURCE_OLD:-}"; DEST="$REPO/svc-2001-old.keytab"; WANT="$KP" ;;
  wrong-principal-keytab)
    SOURCE="${OP04_SOURCE_WRONG:-}"; DEST="$REPO/svc-2002.keytab"; WANT="$DECOY" ;;
  missing-keytab)
    DEST="$REPO/does-not-exist.keytab"
    [[ ! -e "$DEST" && ! -L "$DEST" ]] || fail "The supposed missing keytab already exists: $DEST. Do not delete it automatically."
    info "PASS: missing path is absent; no file staging required."
    exit 0 ;;
esac

# Validate local source by metadata only. Do not run 'cat' or print keys.
[[ -n "$SOURCE" ]] || fail "Set the appropriate OP04_SOURCE_NEW / OP04_SOURCE_OLD / OP04_SOURCE_WRONG to an admin-provided local keytab path"
[[ -f "$SOURCE" && -r "$SOURCE" && -s "$SOURCE" ]] || fail "Keytab source must exist, be readable, and be nonempty"
command -v klist >/dev/null || fail "klist is required for safe principal/KVNO metadata validation"
# MIT klist -k -t displays key version and principal, never actual keys.
ENTRIES="$(klist -k -t "$SOURCE" 2>/dev/null)" || fail "Cannot inspect keytab metadata with klist -k -t"
printf '%s\n' "$ENTRIES" | grep -Fq -- "$WANT" || fail "Keytab does not contain expected Principal $WANT"
KEY_KVNO="$(printf '%s\n' "$ENTRIES" | awk '$1 ~ /^[0-9]+$/ {print $1}' | sort -n | tail -1)"
[[ -n "$KEY_KVNO" ]] || fail "Could not determine keytab KVNO"
OBS_KVNO="$(kc get "principals.kerberator.dell.com/$CR" -o jsonpath='{.status.kvnoFromSecret}')"
info "Validated keytab identity '$WANT', keytab KVNO=$KEY_KVNO (no key bytes read or printed)"
case "$FIXTURE" in
  valid-newer-keytab)
    [[ "$OBS_KVNO" =~ ^[0-9]+$ ]] || fail "Current observed KVNO is unavailable; cannot confirm a newer file"
    (( KEY_KVNO > OBS_KVNO )) || fail "New keytab KVNO must exceed the current Secret KVNO" ;;
  lower-kvno-keytab)
    [[ "$OBS_KVNO" =~ ^[0-9]+$ ]] || fail "Current observed KVNO is unavailable; cannot confirm older file"
    (( KEY_KVNO < OBS_KVNO )) || fail "Old keytab KVNO must be below the current Secret KVNO" ;;
esac

# Verify the destination path is ignored AND not already tracked; no overwrites.
REL="${DEST#"$REPO/"}"
if git -C "$REPO" ls-files --error-unmatch -- "$REL" >/dev/null 2>&1; then
  fail "Destination is tracked by Git: $REL"
fi
if ! git -C "$REPO" check-ignore -q -- "$REL"; then
  fail "Destination is not git-ignored: $REL. Add a safe ignore rule first."
fi
[[ ! -e "$DEST" && ! -L "$DEST" ]] || fail "Destination already exists: $DEST. Refusing overwrite; inspect it manually."

if [[ "$MODE" == --check ]]; then
  info "PASS: fixture '$FIXTURE' is ready to stage at $REL; no changes made."
  exit 0
fi

# Exclusive creation prevents accidental overwrites and follows symlink policy.
# Use install from a local file with restrictive permissions; no shell echo of secrets.
( set -C; umask 077; cat <"$SOURCE" >"$DEST" ) || fail "Unable to create fixture file (destination may already exist)"
chmod 600 "$DEST"
info "PASS: fixture '$FIXTURE' staged at $REL (mode 600, git-ignored)."
info "No Kubernetes resources or KDC credentials were modified by this script."

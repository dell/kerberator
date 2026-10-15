# shellcheck shell=bash disable=SC2034  # variables are used by the scripts that source this
# lib.sh: shared settings and helpers for the DG-03 test scripts.
# Sourced by the other scripts; not meant to be run directly.
#
# Everything targets the `make quickstart` environment: kind cluster,
# namespace kerberator-demo, Tenant demo, in-cluster MIT KDC for realm
# EXAMPLE.COM, Principal svc-2001. Never point these scripts at a real KDC.

set -euo pipefail

NS="${DG03_NS:-kerberator-demo}"
CR="svc-2001"                         # Principal CR name
KP="svc-2001@EXAMPLE.COM"             # Kerberos principal
DECOY_KP="svc-2002@EXAMPLE.COM"       # used only by T2
SECRET="svc-2001-keytab"
SECRET_KEY="svc-2001.keytab"
TENANT="demo"
DS="${TENANT}-kerberator-daemon"

# Work dir lives outside the repo so the agent under test does not stumble
# on the answer key while exploring the project.
WORK_DIR="${DG03_WORK_DIR:-$HOME/.dg03-work}"
KEY_DIR="$WORK_DIR/answer-key"
RUN_DIR="$WORK_DIR/runs"

die()  { echo "error: $*" >&2; exit 2; }
step() { echo "== $*"; }
now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

kc()  { kubectl -n "$NS" "$@"; }
kdc() { kc exec deploy/kdc -- "$@"; }

# Safety rail: refuse to run anywhere but a kind cluster with the demo KDC.
guard_env() {
  command -v kubectl >/dev/null || die "kubectl not on PATH"
  local ctx; ctx="$(kubectl config current-context)"
  [[ $ctx == kind-* ]] || die "current context is '$ctx'; these tests only run on a kind cluster (make quickstart)"
  kc get deploy/kdc >/dev/null 2>&1 || die "no demo KDC in $NS; run 'make quickstart' first"
}

# kdc_kvno <principal>: the KDC's current KVNO for a principal.
kdc_kvno() {
  kdc kadmin.local -q "getprinc $1" 2>/dev/null \
    | awk '/^Key: vno/ { gsub(",", "", $3); print $3; exit }'
}

# keytab_listing <path inside KDC pod>: entries without key material.
keytab_listing() { kdc klist -k -t -e "$1"; }

# load_secret <path inside KDC pod>: replace the Principal's keytab Secret
# with that file. Setup only; the agent must never do this.
load_secret() {
  local tmp; tmp="$(mktemp)"
  kdc cat "$1" >"$tmp"
  kc create secret generic "$SECRET" --from-file="$SECRET_KEY=$tmp" \
    --dry-run=client -o yaml | kc apply -f - >/dev/null
  rm -f "$tmp"
}

# remint: restart the daemons so every node runs kinit with the current Secret.
remint() {
  kc rollout restart "daemonset/$DS" >/dev/null
  kc rollout status "daemonset/$DS" --timeout=120s >/dev/null
}

principal_field() { kc get "principals.kerberator.dell.com/$CR" -o "jsonpath=$1"; }

# wait_for <description> <seconds> <command...>: poll until command succeeds.
wait_for() {
  local what="$1" secs="$2"; shift 2
  for ((i = 0; i < secs; i += 3)); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 3
  done
  echo "  WARN  timed out after ${secs}s waiting for: $what" >&2
  return 1
}

nodes_failing() { [[ $(principal_field '{.status.nodes[*].reason}') == *MintFailed* ]]; }
secret_kvno_is() { [[ $(principal_field '{.status.kvnoFromSecret}') == "$1" ]]; }
principal_ready() { [[ $(principal_field '{.status.conditions[?(@.type=="Ready")].status}') == True ]]; }

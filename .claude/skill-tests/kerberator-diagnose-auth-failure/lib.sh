# shellcheck shell=bash disable=SC2034  # variables are used by the scripts that source this
# lib.sh: shared settings and helpers for the DG-06 test scripts.
# Sourced by the other scripts; not meant to be run directly.
#
# Everything targets the `make quickstart` environment: kind cluster
# kind-kerberator, namespace kerberator-demo, Tenant demo, in-cluster MIT KDC
# for realm EXAMPLE.COM, Principal svc-2001. Never point these at a real KDC.
#
# These scripts never read Secret data. The only Secret fields they read are
# metadata.resourceVersion values, which carry no key material. Keytabs are
# written to the Secret by piping `kubectl create --dry-run | kubectl apply`,
# with output discarded.

set -euo pipefail

REQUIRED_CONTEXT="kind-kerberator"
NS="kerberator-demo"
CR="svc-2001"                         # Principal CR name
KP="svc-2001@EXAMPLE.COM"             # Kerberos principal
WRONG_REALM="WRONG.REALM"             # used only by d3, on the Tenant
DECOY_KP="svc-2002@EXAMPLE.COM"       # used only by d4
SECRET="svc-2001-keytab"
SECRET_KEY="svc-2001.keytab"
TENANT="demo"
DS="${TENANT}-kerberator-daemon"
AGG_SECRET="${TENANT}-keytabs"        # the operator's aggregate of keytab Secrets

_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(git -C "$_LIB_DIR" rev-parse --show-toplevel)"
COLLECT="$REPO/.claude/skills/kerberator-diagnose-auth-failure/scripts/collect.sh"
QUICKSTART_TENANT="$REPO/examples/quickstart/tenant.yaml"  # known-good Tenant spec
ROSTER_CM="${TENANT}-roster"          # the operator copies krb5Conf into this

# Work dir lives outside the repo so the agent under test does not stumble
# on the answer key while exploring the project.
WORK_DIR="${DG06_WORK_DIR:-$HOME/.dg06-work}"
KEY_DIR="$WORK_DIR/answer-key"
RESULT_DIR="$WORK_DIR/results"
RUN_DIR="$WORK_DIR/runs"

die()  { echo "error: $*" >&2; exit 2; }
step() { echo "== $*"; }
now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

kc()  { kubectl -n "$NS" "$@"; }
kdc() { kc exec deploy/kdc -- "$@"; }

# Safety rail: refuse to run anywhere but the quickstart cluster.
guard_env() {
  command -v kubectl >/dev/null || die "kubectl not on PATH"
  local ctx; ctx="$(kubectl config current-context)"
  [[ $ctx == "$REQUIRED_CONTEXT" ]] \
    || die "current context is '$ctx'; these tests only run on $REQUIRED_CONTEXT"
  kc get deploy/kdc >/dev/null 2>&1 || die "no demo KDC in $NS; run 'make quickstart' first"
}

# kdc_kvno <principal>: the KDC's current KVNO, "absent" if the principal does
# not exist, "unavailable" if the KDC can't be reached (d1).
kdc_kvno() {
  local out
  out="$(kdc kadmin.local -q "getprinc $1" 2>/dev/null)" || { echo unavailable; return 0; }
  local v; v="$(awk '/^Key: vno/ { gsub(",", "", $3); print $3; exit }' <<<"$out")"
  echo "${v:-absent}"
}

# keytab_listing <path inside KDC pod>: entries without key material.
keytab_listing() { kdc klist -k -t -e "$1"; }

secret_rv() { kc get secret "$1" -o 'jsonpath={.metadata.resourceVersion}'; }
_rv_differs() { [[ $(secret_rv "$1") != "$2" ]]; }

# load_secret <path inside KDC pod>: replace the Principal's keytab Secret with
# that file, then wait until the operator has re-aggregated it. The daemon
# mounts the aggregate, so re-minting sooner would use the old keytab and show
# the wrong symptom. Setup only; the agent must never do this.
load_secret() {
  local tmp agg_rv; tmp="$(mktemp)"
  agg_rv="$(secret_rv "$AGG_SECRET")"
  kdc cat "$1" >"$tmp"
  kc create secret generic "$SECRET" --from-file="$SECRET_KEY=$tmp" \
    --dry-run=client -o yaml | kc apply -f - >/dev/null
  rm -f "$tmp"
  wait_for "operator to re-aggregate $AGG_SECRET" 60 _rv_differs "$AGG_SECRET" "$agg_rv" || true
}

# remint: restart the daemons so every node runs kinit with the current Secret.
# The daemon only re-mints on start, on a roster change, or every 30 minutes.
# A daemon with no ticket file stays unready, so a timeout here is not fatal;
# callers check the outcome themselves.
remint() {
  kc rollout restart "daemonset/$DS" >/dev/null
  kc rollout status "daemonset/$DS" --timeout=120s >/dev/null 2>&1 \
    || echo "  note: daemon rollout not Ready after 120s (expected if no ticket file exists yet)"
}

principal_field() { kc get "principals.kerberator.dell.com/$CR" -o "jsonpath=$1"; }

# wait_for <description> <seconds> <command...>: poll until command succeeds.
wait_for() {
  local what="$1" secs="$2" i; shift 2
  for ((i = 0; i < secs; i += 3)); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 3
  done
  echo "  WARN  timed out after ${secs}s waiting for: $what" >&2
  return 1
}

nodes_failing()   { [[ $(principal_field '{.status.nodes[*].reason}') == *MintFailed* ]]; }
secret_kvno_is()  { [[ $(principal_field '{.status.kvnoFromSecret}') == "$1" ]]; }
principal_ready() { [[ $(principal_field '{.status.conditions[?(@.type=="Ready")].status}') == True ]]; }
kdc_answers()     { kdc kadmin.local -q listprincs >/dev/null 2>&1; }

# nodes_minted_since <utc-timestamp>: every node reports Minted from a mint at
# or after the timestamp, so a stale Minted from before a restart doesn't count.
nodes_minted_since() {
  local rows r t
  rows="$(principal_field '{range .status.nodes[*]}{.reason}|{.lastObserved}{"\n"}{end}')"
  [[ -n $rows ]] || return 1
  while IFS='|' read -r r t; do
    [[ -n $r ]] || continue
    [[ $r == Minted && ! $t < $1 ]] || return 1
  done <<<"$rows"
}

# nodes_observed_since <utc-timestamp>: every node reports a mint attempt, of
# either outcome, from at or after the timestamp.
nodes_observed_since() {
  local rows r t
  rows="$(principal_field '{range .status.nodes[*]}{.reason}|{.lastObserved}{"\n"}{end}')"
  [[ -n $rows ]] || return 1
  while IFS='|' read -r r t; do
    [[ -n $r ]] || continue
    [[ $r == Minted || $r == *MintFailed* ]] || return 1
    [[ ! $t < $1 ]] || return 1
  done <<<"$rows"
}

tenant_field() { kc get "tenants.kerberator.dell.com/$TENANT" -o "jsonpath=$1"; }

# known_good_tenant: the quickstart Tenant as JSON. jq keeps krb5Conf's trailing
# newline, which a shell command substitution would strip.
known_good_tenant() { kubectl create --dry-run=client -f "$QUICKSTART_TENANT" -o json; }

# restore_tenant: set realm and krb5Conf back to the quickstart values. A merge
# patch leaves other fields alone, and changes nothing if they already match.
restore_tenant() {
  local patch
  patch="$(known_good_tenant | jq -c '{spec: {realm: .spec.realm, daemon: {krb5Conf: .spec.daemon.krb5Conf}}}')"
  kc patch "tenants.kerberator.dell.com/$TENANT" --type=merge -p "$patch" >/dev/null
}

# tenant_restored: the Tenant's realm and krb5Conf, and the krb5.conf the
# operator generated into the roster ConfigMap, all match the quickstart values.
tenant_restored() {
  local want_realm want_conf
  want_realm="$(known_good_tenant | jq -r '.spec.realm')"
  want_conf="$(known_good_tenant | jq -c '.spec.daemon.krb5Conf')"
  [[ $(tenant_field '{.spec.realm}') == "$want_realm" ]] || return 1
  [[ $(kc get "tenants.kerberator.dell.com/$TENANT" -o json | jq -c '.spec.daemon.krb5Conf') == "$want_conf" ]] || return 1
  [[ $(kc get configmap "$ROSTER_CM" -o json | jq -c '.data["krb5.conf"]') == "$want_conf" ]]
}

# krb_list_shows_1of1: `kubectl krb list` reports the Principal Ready, NODES 1/1.
krb_list_shows_1of1() {
  kubectl krb list -n "$NS" | awk -v cr="$CR" '$2 == "Principal" && $3 == cr && $5 == "True" && $6 == "1/1" { found = 1 } END { exit !found }'
}

# daemon_counts: "<ready>/<desired>" for the Tenant's DaemonSet.
daemon_counts() {
  kc get "daemonset/$DS" -o 'jsonpath={.status.numberReady}/{.status.desiredNumberScheduled}'
}

# healthy_1of1: Ready=True, every node Minted, and all daemon pods ready.
healthy_1of1() {
  principal_ready || return 1
  local c; c="$(daemon_counts)"
  [[ ${c%/*} == "${c#*/}" && ${c%/*} -ge 1 ]] || return 1
  [[ $(principal_field '{.status.nodes[*].reason}') != *MintFailed* ]]
}

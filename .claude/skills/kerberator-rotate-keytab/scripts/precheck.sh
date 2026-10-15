#!/usr/bin/env bash
# scripts/precheck.sh <principal> --from-file <path> [-n <ns>]
#
# Read-only. Checks everything `kubectl krb rotate-keytab` needs and records
# the "before" state. Never reads Secret data and never prints key material:
# the keytab file is inspected only with `klist -k -t` or `ktutil list`
# (principal + KVNO + enctype).
#
# Exit 0 = ready (possibly with warnings), 1 = blocked, 2 = usage/env error.
set -o pipefail
source "$(dirname "$0")/_common.sh"
parse_args "$@"
[[ -n $FROM ]] || die "missing --from-file <path to the new keytab>"
env_check

echo "== Precheck for rotating $NAME ($(now_utc)) =="

echo "Permissions:"
for check in "get principals.kerberator.dell.com" "get secrets" "update secrets" \
             "get daemonsets.apps" "update daemonsets.apps"; do
  # shellcheck disable=SC2086  # $check is deliberately split into verb + resource
  if [[ $(kc auth can-i $check 2>/dev/null) == yes ]]; then ok "can $check"; else fail "cannot $check (rotate-keytab needs it)"; fi
done
if [[ $(kc auth can-i list events 2>/dev/null) == yes ]]; then ok "can list events"
else warn "cannot list events (verify.sh will skip the mint-failure check)"; fi

echo "Principal:"
row="$(kc get principals.kerberator.dell.com "$NAME" -o \
  'jsonpath={.spec.uid}|{.spec.principal}|{.spec.tenantRef.name}|{.spec.keytabSecretRef.name}|{.spec.keytabSecretRef.key}|{.status.kvnoFromSecret}|{.status.keytabObservedAt}|{.spec.disabled}' 2>&1)"
if [[ $? -ne 0 ]]; then
  fail "cannot read Principal $NAME: $row"
  echo "RESULT: BLOCKED"; exit 1
fi
IFS='|' read -r uid kprinc tenant secret key kvno observed disabled <<<"$row"
ok "Principal $NAME exists (uid=$uid, principal=$kprinc, tenant=$tenant)"
if [[ -z $secret || -z $key ]]; then
  fail "Principal has no keytabSecretRef; there is nothing to rotate"
else
  ok "keytabSecretRef: $secret / key $key"
  if kc get secret "$secret" -o name >/dev/null 2>&1; then ok "Secret $secret exists"
  else fail "Secret $secret not found (or not readable)"; fi
fi
info "current KVNO in Secret: ${kvno:-0}${observed:+ (observed $observed)}"
[[ $disabled == true ]] && warn "Principal is disabled; daemons will not mint it, so the rotation cannot be verified on nodes"

echo "Daemons:"
ds="$tenant-kerberator-daemon"
ready="$(kc get daemonset "$ds" -o 'jsonpath={.status.numberReady}/{.status.desiredNumberScheduled}' 2>&1)"
if [[ $? -ne 0 ]]; then fail "DaemonSet $ds not readable: $ready"
elif [[ ${ready%/*} == "${ready#*/}" ]]; then ok "DaemonSet $ds ready $ready"
else warn "DaemonSet $ds only $ready ready; nodes without a ready daemon will not re-mint"; fi
nodes="$(principal_field '{range .status.nodes[*]}{.name}={.reason} {end}')"
info "node status now: ${nodes:-<none reported>}"

echo "New keytab file:"
if [[ ! -f $FROM || ! -r $FROM ]]; then
  fail "$FROM does not exist or is not readable"
elif [[ ! -s $FROM ]]; then
  fail "$FROM is empty"
else
  ok "$FROM is readable ($(wc -c <"$FROM" | tr -d ' ') bytes)"
  if entries="$(keytab_entries "$FROM")"; then
    new_kvno="$(awk '$1 ~ /^[0-9]+$/ {print $1}' <<<"$entries" | sort -n | tail -1)"
    if grep -qF -- "$kprinc" <<<"$entries"; then ok "keytab contains $kprinc"
    else fail "keytab does not contain $kprinc (wrong file?)"; fi
    if [[ -z $new_kvno ]]; then warn "could not read a KVNO from the keytab listing"
    elif [[ -z $kvno || $kvno == 0 ]]; then info "new keytab KVNO: $new_kvno (current KVNO unknown)"
    elif (( new_kvno > kvno )); then ok "new keytab KVNO $new_kvno > current $kvno"
    elif (( new_kvno == kvno )); then warn "new keytab KVNO $new_kvno equals current; this may be the same key, and keytabObservedAt will not refresh"
    else warn "new keytab KVNO $new_kvno is LOWER than current $kvno; the KDC likely rejects it"; fi
  elif command -v klist >/dev/null || command -v ktutil >/dev/null; then
    fail "$FROM could not be read as a keytab (klist/ktutil found no entries); rotating it would break logins"
  else
    warn "neither klist (MIT) nor ktutil (Heimdal) is installed; principal and KVNO unverified"
  fi
fi

echo "Before state (pass to verify.sh):"
echo "  before_kvno=${kvno:-0}"
echo "  expected_kvno=${new_kvno:-unknown}"
echo "  checked_at=$(now_utc)"

if (( FAILED )); then echo "RESULT: BLOCKED"; exit 1; fi
if (( WARNED )); then echo "RESULT: READY WITH WARNINGS"; else echo "RESULT: READY"; fi

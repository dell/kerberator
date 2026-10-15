#!/usr/bin/env bash
# scripts/verify.sh <principal> --since <started_at> [--expect-kvno N] [-n <ns>]
#
# Read-only. Checks the documented post-rotation state independently of the
# CLI's own wait loop:
#   1. every node in Principal.status.nodes reports Minted, observed at or
#      after the rotation started
#   2. the Principal's Ready condition is True
#   3. the operator observed the expected KVNO in the Secret
#   4. no KerberosMintFailed events for this UID since the rotation started
#
# Exit 0 = VERIFIED, 1 = NOT VERIFIED, 2 = usage/env error.
set -o pipefail
source "$(dirname "$0")/_common.sh"
parse_args "$@"
[[ -n $SINCE ]] || die "missing --since <started_at printed by rotate.sh>"
env_check

echo "== Verifying rotation of $NAME since $SINCE ($(now_utc)) =="

uid="$(principal_field '{.spec.uid}')" || true
[[ -n $uid ]] || { fail "cannot read Principal $NAME"; echo "RESULT: NOT VERIFIED"; exit 1; }

echo "Nodes:"
nodes="$(principal_field '{range .status.nodes[*]}{.name}|{.reason}|{.lastObserved}{"\n"}{end}')"
if [[ -z $nodes ]]; then
  fail "no node status reported"
else
  while IFS='|' read -r node reason seen; do
    [[ -n $node ]] || continue
    if [[ $reason != Minted ]]; then fail "$node: ${reason:-no status} (observed ${seen:-never})"
    elif ! ts_ge "$seen" "$SINCE"; then fail "$node: Minted, but last observed $seen is before the rotation"
    else ok "$node: Minted at $seen"; fi
  done <<<"$nodes"
fi

echo "Principal condition:"
cond="$(principal_field '{.status.conditions[?(@.type=="Ready")].status}|{.status.conditions[?(@.type=="Ready")].reason}|{.status.conditions[?(@.type=="Ready")].message}')"
IFS='|' read -r cstatus creason cmsg <<<"$cond"
if [[ $cstatus == True ]]; then ok "Ready=True"
else fail "Ready=${cstatus:-missing} ${creason:+($creason)} ${cmsg}"; fi

echo "Keytab as observed by the operator:"
kvno="$(principal_field '{.status.kvnoFromSecret}')"
observed="$(principal_field '{.status.keytabObservedAt}')"
if [[ -z $kvno || $kvno == 0 ]]; then
  fail "KVNO is 0: the operator could not parse the keytab Secret (missing, empty, or corrupt)"
elif [[ -n $EXPECT_KVNO && $EXPECT_KVNO != unknown ]]; then
  if [[ $kvno == "$EXPECT_KVNO" ]]; then ok "KVNO $kvno matches the new keytab"
  else fail "KVNO is $kvno, expected $EXPECT_KVNO (operator may not have reconciled yet, or the wrong file was used)"; fi
else
  info "KVNO $kvno (no --expect-kvno given, not compared)"
fi
if ts_ge "$observed" "$SINCE"; then ok "keytabObservedAt $observed is after the rotation"
else info "keytabObservedAt ${observed:-unset} not refreshed (normal if the KVNO and enctypes did not change)"; fi

echo "Events:"
if [[ $(kc auth can-i list events 2>/dev/null) != yes ]]; then
  warn "cannot list events; mint-failure check skipped"
else
  failures="$(kc get events -o \
    'jsonpath={range .items[?(@.reason=="KerberosMintFailed")]}{.metadata.annotations.kerberator\.dell\.com/uid}|{.lastTimestamp}|{.eventTime}|{.message}{"\n"}{end}' 2>/dev/null)"
  found=0
  # '|' rather than a tab: bash merges consecutive tabs, which would
  # shift fields when lastTimestamp is empty.
  while IFS='|' read -r euid last etime msg; do
    [[ $euid == "$uid" ]] || continue
    # Newer Events set eventTime and leave lastTimestamp null, older ones the
    # reverse; jsonpath prints the unset one as "null".
    [[ $last == null ]] && last=""
    [[ $etime == null ]] && etime=""
    when="${last:-$etime}"
    ts_ge "$when" "$SINCE" || continue
    fail "KerberosMintFailed at $when: $msg"
    found=1
  done <<<"$failures"
  (( found )) || ok "no KerberosMintFailed events for uid $uid since $SINCE"
fi

if (( FAILED )); then echo "RESULT: NOT VERIFIED"; exit 1; fi
echo "RESULT: VERIFIED"

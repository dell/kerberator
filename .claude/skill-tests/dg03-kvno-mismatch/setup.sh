#!/usr/bin/env bash
# setup.sh <reset|t1|t2|t3|t4>
#
# Puts the quickstart environment into one DG-03 scenario and records the
# expected values (the answer key) before the agent ever runs. Run by the
# tester, never by the agent.
#
#   reset  KDC and Secret agree again, Principal Ready, T2/T4 leftovers removed
#   t1     real KVNO mismatch: KDC re-keyed, Secret still holds the old key
#   t2     decoy: Secret holds a different principal's keytab (svc-2002)
#   t3     control: KVNO matches the KDC but the key is wrong
#   t4     same as t1, but the agent gets a kubeconfig with no KDC access
source "$(dirname "$0")/lib.sh"

[[ $# -eq 1 ]] || die "usage: setup.sh <reset|t1|t2|t3|t4>"
TEST="$1"
guard_env
mkdir -p "$KEY_DIR"

reset() {
  step "reset: export a fresh keytab and make the Secret match the KDC"
  kdc rm -f /tmp/dg03-fresh.keytab
  kdc kadmin.local -q "ktadd -k /tmp/dg03-fresh.keytab $KP" >/dev/null
  load_secret /tmp/dg03-fresh.keytab
  kdc kadmin.local -q "delprinc -force $DECOY_KP" >/dev/null 2>&1 || true
  kc delete rolebinding,role,serviceaccount -l dg03-test=t4 --ignore-not-found >/dev/null
  remint
  wait_for "Principal $CR Ready" 180 principal_ready
  echo "  KDC KVNO now $(kdc_kvno "$KP"); Principal Ready=$(principal_field '{.status.conditions[?(@.type=="Ready")].status}')"
}

# record <test> <keytab path in KDC pod>: write the answer key.
record() {
  local t="$1" kt="$2" f="$KEY_DIR/$1.txt" want
  want="$(keytab_listing "$kt" | awk '$1 ~ /^[0-9]+$/ {print $1}' | sort -n | tail -1)"
  wait_for "operator to observe KVNO $want in the Secret" 60 secret_kvno_is "$want" || true
  wait_for "nodes to report MintFailed" 180 nodes_failing || true
  {
    echo "test:                 $t"
    echo "recorded_at:          $(now_utc)"
    echo "configured_principal: $(principal_field '{.spec.principal}')"
    echo "kdc_kvno($KP): $(kdc_kvno "$KP")"
    if [[ $t == t2 ]]; then echo "kdc_kvno($DECOY_KP): $(kdc_kvno "$DECOY_KP")"; fi
    echo "status.kvnoFromSecret: $(principal_field '{.status.kvnoFromSecret}')"
    echo "status.enctypesFromSecret: $(principal_field '{.status.enctypesFromSecret}')"
    echo "node reasons:         $(principal_field '{range .status.nodes[*]}{.name}={.reason} {end}')"
    echo "keytab entries in Secret (klist -k -t -e, no keys):"
    keytab_listing "$kt" | sed 's/^/  /'
    echo "mint failure messages:"
    kc get events --field-selector reason=KerberosMintFailed \
      -o jsonpath='{range .items[*]}{.message}{"\n"}{end}' | sort -u | tail -5 | sed 's/^/  /'
  } >"$f"
  step "answer key written to $f"
  cat "$f"
}

case "$TEST" in
  reset) reset ;;

  t1|t4)
    reset
    step "$TEST: re-key $KP on the KDC without updating the Secret"
    kdc kadmin.local -q "cpw -randkey $KP" >/dev/null
    remint
    record "$TEST" /tmp/dg03-fresh.keytab
    if [[ $TEST == t4 ]]; then
      step "t4: create a read-only kubeconfig with no exec (so no KDC access)"
      "$(dirname "$0")/make-readonly-kubeconfig.sh" "$WORK_DIR/t4.kubeconfig"
    fi
    ;;

  t2)
    reset
    step "t2: put $DECOY_KP's keytab into $SECRET and re-key $KP so the KVNOs differ"
    kdc kadmin.local -q "addprinc -randkey $DECOY_KP" >/dev/null
    kdc rm -f /tmp/dg03-t2.keytab
    kdc kadmin.local -q "ktadd -k /tmp/dg03-t2.keytab $DECOY_KP" >/dev/null
    kdc kadmin.local -q "cpw -randkey $KP" >/dev/null
    while [[ $(kdc_kvno "$KP") == "$(kdc_kvno "$DECOY_KP")" ]]; do
      kdc kadmin.local -q "cpw -randkey $KP" >/dev/null
    done
    load_secret /tmp/dg03-t2.keytab
    remint
    record t2 /tmp/dg03-t2.keytab
    ;;

  t3)
    reset
    kvno="$(kdc_kvno "$KP")"
    step "t3: build a keytab for $KP at the KDC's KVNO $kvno, but from a wrong password"
    kdc sh -c "rm -f /tmp/dg03-t3.keytab; printf '%s\n' \
      'addent -password -p $KP -k $kvno -e aes256-cts-hmac-sha1-96' 'dg03-wrong-password' \
      'addent -password -p $KP -k $kvno -e aes128-cts-hmac-sha1-96' 'dg03-wrong-password' \
      'wkt /tmp/dg03-t3.keytab' 'quit' | ktutil >/dev/null"
    load_secret /tmp/dg03-t3.keytab
    remint
    record t3 /tmp/dg03-t3.keytab
    ;;

  *) die "unknown test '$TEST' (want reset, t1, t2, t3 or t4)" ;;
esac

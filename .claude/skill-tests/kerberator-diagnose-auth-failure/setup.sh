#!/usr/bin/env bash
# setup.sh <reset|d1|d2|d3|d4|d5>
#
# Puts the quickstart environment into one DG-06 scenario and records the
# answer key before the agent ever runs. Run by the tester, never by the agent.
#
#   reset  KDC up, principal present, Secret matches the KDC, Principal Ready 1/1
#   d1     KDC unreachable: deploy/kdc scaled to 0, re-mint forced
#   d2     principal missing: svc-2001@EXAMPLE.COM deleted on the KDC, re-mint forced
#   d3     wrong realm, on the Tenant: spec.realm set to WRONG.REALM, re-mint forced
#   d4    decoy: svc-2002's keytab in svc-2001's Secret, svc-2001 re-keyed
#   d5     control: nothing broken
#
# Exit 0 = done, 2 = usage or environment error, 3 = the scenario could not be
# established (the answer key says why; do not run the agent against it).
#
# Why reset always re-exports the keytab: the demo KDC's database is on the
# container filesystem (/var/lib/krb5kdc), not the emptyDir, and bootstrap.sh
# creates a new realm with new random keys on every container start. So scaling
# back up, or any KDC restart, leaves the Secret holding a key the KDC no longer
# accepts, and also brings back the principal d2 deleted.
source "$(dirname "$0")/lib.sh"

[[ $# -eq 1 ]] || die "usage: setup.sh <reset|d1|d2|d3|d4|d5>"
TEST="$1"
guard_env
mkdir -p "$KEY_DIR"

reset() {
  local since kv attempt
  step "reset: restore Tenant realm and krb5.conf, KDC, principal and Secret"
  # Before the re-mint below: the daemon mounts krb5.conf with subPath, so a
  # running pod never sees a change and only a new pod picks it up.
  restore_tenant
  wait_for "Tenant realm and generated krb5.conf to be restored" 60 tenant_restored \
    || die "Tenant realm or generated krb5.conf not restored"
  kc scale deploy/kdc --replicas=1 >/dev/null
  kc rollout status deploy/kdc --timeout=180s >/dev/null
  wait_for "KDC database to answer" 120 kdc_answers || die "KDC did not come back"

  if [[ $(kdc_kvno "$KP") == absent ]]; then
    kdc kadmin.local -q "addprinc -randkey $KP" >/dev/null 2>&1
  fi
  kdc kadmin.local -q "delprinc -force $DECOY_KP" >/dev/null 2>&1 || true

  kdc rm -f /tmp/dg06-fresh.keytab
  kdc kadmin.local -q "ktadd -k /tmp/dg06-fresh.keytab $KP" >/dev/null 2>&1
  kv="$(kdc_kvno "$KP")"
  load_secret /tmp/dg06-fresh.keytab
  kdc rm -f /tmp/dg06-fresh.keytab
  wait_for "operator to observe KVNO $kv" 60 secret_kvno_is "$kv" || true

  since="$(now_utc)"
  for attempt in 1 2 3; do
    remint
    if wait_for "a fresh Minted on every node" 120 nodes_minted_since "$since" \
       && wait_for "Principal $CR Ready 1/1" 120 healthy_1of1 \
       && wait_for "kubectl krb list to show $CR Ready, NODES 1/1" 60 krb_list_shows_1of1; then
      break
    fi
    [[ $attempt -lt 3 ]] || die "Principal $CR not healthy after 3 re-mints"
    echo "  re-mint attempt $attempt did not reach healthy; retrying"
  done
  tenant_restored || die "Tenant realm or generated krb5.conf changed during reset"
  echo "  healthy: KDC KVNO $(kdc_kvno "$KP"), Secret KVNO $(principal_field '{.status.kvnoFromSecret}'), Ready=$(principal_field '{.status.conditions[?(@.type=="Ready")].status}'), daemon pods $(daemon_counts), Tenant realm $(tenant_field '{.spec.realm}'), krb5.conf matches quickstart"
}

# key_header <test> <fault> <established> <predicted signature> <predicted realm_match>
key_header() {
  echo "test:                      $1"
  echo "recorded_at:               $(now_utc)"
  echo "fault_injected:            $2"
  echo "established:               $3"
  echo "predicted_error_signature: $4"
  echo "predicted_realm_match:     $5"
}

# record <test> <fault> <predicted signature> <predicted realm_match> [keytab path in KDC pod]
# Predictions are written down here, before collect.sh runs, and capture.sh
# compares the skill's real output with them. They come from reading
# kinit-errors.md and collect.sh, so a mismatch is a finding, not an error.
record() {
  local t="$1" fault="$2" sig="$3" realm="$4" kt="${5:-}" f="$KEY_DIR/$1.txt"
  {
    key_header "$t" "$fault" yes "$sig" "$realm"
    echo "configured_principal:      $(principal_field '{.spec.principal}')"
    echo "tenant_realm:              $(tenant_field '{.spec.realm}')"
    echo "generated_krb5_default_realm: $(kc get configmap "$ROSTER_CM" -o json | jq -r '.data["krb5.conf"]' | awk '/default_realm/ {print $3; exit}')"
    echo "kdc_replicas:              $(kc get deploy/kdc -o 'jsonpath={.spec.replicas}')"
    echo "kdc_kvno($KP): $(kdc_kvno "$KP")"
    [[ $t != d4 ]] || echo "kdc_kvno($DECOY_KP): $(kdc_kvno "$DECOY_KP")"
    echo "status.kvnoFromSecret:     $(principal_field '{.status.kvnoFromSecret}')"
    echo "status.enctypesFromSecret: $(principal_field '{.status.enctypesFromSecret}')"
    echo "node_reasons:              $(principal_field '{range .status.nodes[*]}{.name}={.reason} {end}')"
    echo "daemon_pods:               $(daemon_counts)"
    if [[ -n $kt ]]; then
      echo "keytab entries in Secret (klist -k -t -e of the source file, no keys):"
      keytab_listing "$kt" | sed 's/^/  /'
    fi
    # Events last about an hour, so these can predate this scenario.
    echo "recent mint failure messages, any scenario in the last hour (newest last):"
    kc get events --field-selector reason=KerberosMintFailed --sort-by=.lastTimestamp \
      -o jsonpath='{range .items[*]}{.message}{"\n"}{end}' | grep -v '^)$' | tail -3 | sed 's/^/  /'
  } >"$f"
  step "answer key written to $f"
  cat "$f"
}

case "$TEST" in
  reset) reset ;;

  d1)
    reset
    step "d1: scale deploy/kdc to 0 and force a re-mint"
    kc scale deploy/kdc --replicas=0 >/dev/null
    kc wait pod -l app=kdc --for=delete --timeout=120s >/dev/null 2>&1 || true
    remint
    wait_for "nodes to report MintFailed" 240 nodes_failing || true
    record d1 "deploy/kdc scaled to 0" kdc-unreachable yes
    ;;

  d2)
    reset
    step "d2: delete $KP on the KDC and force a re-mint"
    kdc kadmin.local -q "delprinc -force $KP" >/dev/null 2>&1
    [[ $(kdc_kvno "$KP") == absent ]] || die "$KP still exists on the KDC"
    remint
    wait_for "nodes to report MintFailed" 180 nodes_failing || true
    record d2 "$KP deleted on the KDC" client-not-found yes
    ;;

  d3)
    reset
    step "d3: set the Tenant's spec.realm to $WRONG_REALM and force a re-mint"
    # The fault is on the Tenant because webhook rule 3
    # (operator/internal/webhook/principal_validation.go) refuses a Principal
    # whose realm differs from its Tenant's. Never remove the webhook or stop the
    # operator to get past it.
    #
    # Only spec.realm changes. The operator copies krb5.conf from
    # spec.daemon.krb5Conf and doesn't use spec.realm to build it, so kinit should
    # still work. Hence the prediction: a mismatch, with no failure.
    if ! out="$(kc patch "tenants.kerberator.dell.com/$TENANT" --type=merge \
                  -p "{\"spec\":{\"realm\":\"$WRONG_REALM\"}}" 2>&1)"; then
      {
        key_header d3 "Tenant spec.realm -> $WRONG_REALM (REJECTED, nothing changed)" no none yes
        echo "reason_not_established:    the patch was refused by the API server"
        echo "patch_output:              $out"
        echo "tenant_realm:              $(tenant_field '{.spec.realm}')"
      } >"$KEY_DIR/d3.txt"
      step "answer key written to $KEY_DIR/d3.txt"
      cat "$KEY_DIR/d3.txt"
      echo "SCENARIO NOT ESTABLISHED: d3 could not be set up; the environment is unchanged and healthy." >&2
      exit 3
    fi
    since="$(now_utc)"
    remint
    wait_for "a mint attempt on every node after the restart" 180 nodes_observed_since "$since" || true
    record d3 "Tenant $TENANT spec.realm -> $WRONG_REALM; the Principal and spec.daemon.krb5Conf are untouched" none no
    ;;

  d4)
    reset
    step "d4: put $DECOY_KP's keytab into $SECRET and re-key $KP so the KVNOs differ"
    kdc kadmin.local -q "addprinc -randkey $DECOY_KP" >/dev/null 2>&1
    kdc rm -f /tmp/dg06-d4.keytab
    kdc kadmin.local -q "ktadd -k /tmp/dg06-d4.keytab $DECOY_KP" >/dev/null 2>&1
    kdc kadmin.local -q "cpw -randkey $KP" >/dev/null 2>&1
    while [[ $(kdc_kvno "$KP") == "$(kdc_kvno "$DECOY_KP")" ]]; do
      kdc kadmin.local -q "cpw -randkey $KP" >/dev/null 2>&1
    done
    load_secret /tmp/dg06-d4.keytab
    want="$(keytab_listing /tmp/dg06-d4.keytab | awk '$1 ~ /^[0-9]+$/ {print $1}' | sort -n | tail -1)"
    wait_for "operator to observe KVNO $want" 60 secret_kvno_is "$want" || true
    remint
    wait_for "nodes to report MintFailed" 180 nodes_failing || true
    record d4 "$SECRET holds $DECOY_KP's keytab; $KP re-keyed" keytab-no-matching-key yes /tmp/dg06-d4.keytab
    kdc rm -f /tmp/dg06-d4.keytab
    ;;

  d5)
    reset
    step "d5: control, nothing broken"
    record d5 "none (control)" none yes
    ;;

  *) die "unknown test '$TEST' (want reset, d1, d2, d3, d4 or d5)" ;;
esac

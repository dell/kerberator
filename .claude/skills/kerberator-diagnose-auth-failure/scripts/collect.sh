#!/usr/bin/env bash
# scripts/collect.sh <principal> [-n <ns>] [--context <ctx>] [--log-lines N]
#
# Read-only. Gathers every piece of evidence the diagnosis needs for one
# Kerberator Principal, in a fixed order, so the agent sees the same complete
# picture every time:
#
#   1. Principal: identity, realm, Tenant, disabled flag, keytab metadata,
#      node selector
#   2. Tenant: realm, KDC configuration in its krb5.conf, ticket cache dir
#   3. Per-node status as the operator reports it
#   4. Daemon pods: one per node, and why any of them isn't running
#   5. Kerberos events for this UID, newest first, with UNTRUNCATED messages
#   6. Daemon log lines for this principal (the full kinit error)
#   7. Every Principal in the namespace (a shared cause fails many at once)
#   8. SIGNALS: one-line facts derived from the above, for the classification
#
# Never reads Secret data, never execs into pods, never changes anything.
# A section the caller may not read prints "UNAVAILABLE: <reason>" and the
# script carries on.
#
# Exit 0 = collected, 2 = usage error, 3 = Principal not found / unreadable.
set -o pipefail

die() { echo "error: $*" >&2; exit 2; }

NAME=""; KARGS=(); LOG_LINES=2000
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--namespace) [[ -n ${2:-} ]] || die "$1 needs a value"; KARGS+=(-n "$2"); shift ;;
    --namespace=*)  KARGS+=(-n "${1#*=}") ;;
    --context)      [[ -n ${2:-} ]] || die "$1 needs a value"; KARGS+=(--context "$2"); shift ;;
    --context=*)    KARGS+=(--context "${1#*=}") ;;
    --log-lines)    [[ ${2:-} =~ ^[0-9]+$ ]] || die "$1 needs a number"; LOG_LINES="$2"; shift ;;
    -*) die "unknown flag: $1" ;;
    *)  [[ -z $NAME ]] || die "unexpected argument: $1"; NAME="$1" ;;
  esac
  shift
done
[[ -n $NAME ]] || die "usage: collect.sh <principal-CR-name> [-n <ns>] [--context <ctx>]"
command -v kubectl >/dev/null || die "kubectl not found on PATH"

kc() { kubectl ${KARGS[@]+"${KARGS[@]}"} "$@"; }
section() { echo; echo "== $* =="; }
# jsonpath prints unset fields as "null"; treat that as empty.
nn() { [[ $1 == null ]] && echo "" || echo "$1"; }

echo "COLLECTED: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "CONTEXT:   $(kc config current-context 2>/dev/null || echo unknown)"

# --- 1. Principal -----------------------------------------------------------
section "1. Principal $NAME"
P='{.spec.uid}|{.spec.principal}|{.spec.tenantRef.name}|{.spec.disabled}|{.spec.disabledReason}|{.spec.keytabSecretRef.name}|{.status.kvnoFromSecret}|{.status.enctypesFromSecret}|{.status.keytabObservedAt}|{.spec.nodeSelector}'
row="$(kc get principals.kerberator.dell.com "$NAME" -o "jsonpath=$P" 2>&1)" || {
  echo "UNAVAILABLE: $row"
  echo "HINT: Principal CR names are not UIDs or Kerberos names; 'kubectl krb list' shows the real names."
  # kubectl plugins need their name before any flags, so no kc() here.
  kubectl krb list ${KARGS[@]+"${KARGS[@]}"} 2>/dev/null | sed 's/^/  /'
  exit 3
}
IFS='|' read -r uid kprinc tenant disabled dreason secret kvno enctypes observed nodesel <<<"$row"
ns="$(kc get principals.kerberator.dell.com "$NAME" -o 'jsonpath={.metadata.namespace}' 2>/dev/null)"
preal="${kprinc##*@}"
echo "  uid:              $uid"
echo "  kerberos name:    $kprinc   (realm: ${preal:-none})"
echo "  namespace:        $ns"
echo "  tenant:           $tenant"
echo "  disabled:         ${disabled:-false}${dreason:+  (reason: $dreason)}"
echo "  keytab secret:    ${secret:-<none set>}   (contents never read)"
echo "  KVNO in secret:   ${kvno:-<not observed>}   observed: ${observed:-never}"
echo "  enctypes:         ${enctypes:-<not observed>}"
echo "  node selector:    ${nodesel:-<none: every node the Tenant serves>}"
echo "  Ready condition:"
kc get principals.kerberator.dell.com "$NAME" -o 'jsonpath={range .status.conditions[*]}    {.type}={.status}  {.reason}: {.message}{"\n"}{end}' 2>/dev/null

# --- 2. Tenant ----------------------------------------------------------------
section "2. Tenant $tenant (realm and KDC configuration)"
trow="$(kc get tenants.kerberator.dell.com "$tenant" -o 'jsonpath={.spec.realm}|{.spec.disabled}|{.spec.daemon.renewMinutes}|{.spec.daemon.hostCachePath}|{.spec.daemon.nodeSelector}' 2>&1)"
if [[ $? -ne 0 ]]; then
  echo "UNAVAILABLE: $trow"; trealm=""
else
  IFS='|' read -r trealm tdisabled renew cachedir tnodesel <<<"$trow"
  echo "  realm:            $trealm"
  echo "  disabled:         ${tdisabled:-false}"
  echo "  renew interval:   ${renew:-default} minutes"
  echo "  ticket cache dir: ${cachedir:-/tmp (default)} on each node, file krb5cc_<uid>"
  echo "  daemon nodes:     ${tnodesel:-<all nodes>}"
  echo "  conditions:"
  kc get tenants.kerberator.dell.com "$tenant" -o 'jsonpath={range .status.conditions[*]}    {.type}={.status}  {.reason}: {.message}{"\n"}{end}' 2>/dev/null
  conf="$(kc get tenants.kerberator.dell.com "$tenant" -o 'jsonpath={.spec.daemon.krb5Conf}' 2>/dev/null)"
  echo "  krb5.conf (realm and KDC lines only):"
  grep -E 'default_realm|^[[:space:]]*[A-Za-z0-9._-]+[[:space:]]*=[[:space:]]*\{|[[:space:]]kdc[[:space:]]*=|admin_server|dns_lookup' <<<"$conf" | sed 's/^/    /'
fi

# --- 3. Per-node status ---------------------------------------------------------
section "3. Per-node status (as the operator reports it)"
nodes="$(kc get principals.kerberator.dell.com "$NAME" -o 'jsonpath={range .status.nodes[*]}{.name}|{.daemonPodReady}|{.reason}|{.lastObserved}|{.message}{"\n"}{end}' 2>/dev/null)"
total=0; failing=0; nodemsg=""
if [[ -z $nodes ]]; then
  echo "  (no nodes reported)"
else
  printf '  %-12s %-6s %-12s %-22s %s\n' NODE READY REASON LAST-OBSERVED MESSAGE
  while IFS='|' read -r n ready reason seen msg; do
    # A kinit message can end in a newline, which puts its closing ")" on a
    # line of its own; only real node rows have a true/false ready field.
    [[ -n $n && ( $ready == true || $ready == false ) ]] || continue
    total=$((total + 1))
    if [[ $reason != Minted ]]; then failing=$((failing + 1)); [[ -n $nodemsg ]] || nodemsg="$msg"; fi
    printf '  %-12s %-6s %-12s %-22s %s\n' "$n" "$ready" "${reason:--}" "${seen:--}" "$msg"
  done <<<"$nodes"
fi

# --- 4. Daemon pods -------------------------------------------------------------
section "4. Daemon pods for tenant $tenant"
sel="app.kubernetes.io/name=kerberator-daemon,app.kubernetes.io/instance=$tenant"
pods="$(kc get pods -l "$sel" -o 'jsonpath={range .items[*]}{.metadata.name}|{.spec.nodeName}|{.status.phase}|{.status.containerStatuses[0].ready}|{.status.containerStatuses[0].restartCount}{"\n"}{end}' 2>&1)"
rc=$?   # capture before any other command overwrites $?
pods_total=0; pods_ready=0; podnames=()
if [[ $rc -ne 0 ]]; then
  echo "UNAVAILABLE: $pods"
else
  printf '  %-34s %-10s %-9s %-6s %s\n' POD NODE PHASE READY RESTARTS
  while IFS='|' read -r pn pnode phase pready restarts; do
    [[ -n $pn ]] || continue
    pods_total=$((pods_total + 1)); [[ $pready == true ]] && pods_ready=$((pods_ready + 1))
    podnames+=("$pn")
    printf '  %-34s %-10s %-9s %-6s %s\n' "$pn" "$pnode" "$phase" "$pready" "$restarts"
    # For a pod that isn't ready or has restarted, show why (scheduling,
    # image pull, crash) from its own Kubernetes events.
    if [[ $pready != true || ${restarts:-0} != 0 ]]; then
      kc get events --field-selector "involvedObject.name=$pn" \
        -o 'jsonpath={range .items[*]}{.lastTimestamp}|{.type}|{.reason}|{.message}{"\n"}{end}' 2>/dev/null \
        | grep '|' | sort -r | head -5 | while IFS='|' read -r when type reason msg; do
            echo "      [$(nn "$when")] $type $reason: $msg"
          done
    fi
  done <<<"$pods"
fi

# --- 5. Kerberos events for this UID ------------------------------------------------
section "5. Kerberos events for uid $uid (newest first, full messages)"
ev="$(kc get events -o 'jsonpath={range .items[?(@.metadata.annotations.kerberator\.dell\.com/uid)]}{.metadata.annotations.kerberator\.dell\.com/uid}|{.lastTimestamp}|{.eventTime}|{.reason}|{.involvedObject.name}|{.count}|{.message}{"\n"}{end}' 2>&1)"
rc=$?
latest_reason=""; latest_msg=""
if [[ $rc -ne 0 ]]; then
  echo "UNAVAILABLE: $ev"
else
  sorted="$(while IFS='|' read -r euid last etime reason pod count msg; do
      [[ $euid == "$uid" ]] || continue
      last="$(nn "$last")"; etime="$(nn "$etime")"
      echo "${last:-$etime}|$reason|$pod|${count:-1}|$msg"
    done <<<"$ev" | sort -r)"
  if [[ -z $sorted ]]; then
    echo "  (none: not reported yet, or expired; Kubernetes keeps events for about an hour)"
  else
    head -20 <<<"$sorted" | while IFS='|' read -r when reason pod count msg; do
      echo "  [$when] $reason on $pod (x$count): $msg"
    done
    IFS='|' read -r _ latest_reason _ _ latest_msg <<<"$(head -1 <<<"$sorted")"
  fi
fi

# --- 6. Daemon logs ------------------------------------------------------------------
section "6. Daemon log lines for $kprinc (last $LOG_LINES lines per pod)"
logerr=""
for pn in ${podnames[@]+"${podnames[@]}"}; do
  out="$(kc logs "$pn" --tail="$LOG_LINES" 2>&1)"
  if [[ $? -ne 0 ]]; then echo "  $pn: UNAVAILABLE: $(head -1 <<<"$out")"; continue; fi
  # The kinit error can wrap onto the next line, so keep one line after each match.
  lines="$(grep -F -A1 -- "$kprinc" <<<"$out" | grep -v '^--$' | tail -8)"
  echo "  --- $pn"
  if [[ -n $lines ]]; then sed 's/^/    /' <<<"$lines"
  else echo "    (no lines mention $kprinc)"; fi
  # Only a pod whose LATEST line for this principal is an error counts as
  # failing now; an old error followed by a success is already resolved.
  if grep -F -- "$kprinc" <<<"$out" | tail -1 | grep -q 'ERROR'; then
    logerr="$(grep -F -A1 -- "$kprinc" <<<"$out" | tail -2 | tr '\n' ' ')"
  fi
done
[[ ${#podnames[@]} -gt 0 ]] || echo "  (no daemon pods found)"

# --- 7. Other Principals in the namespace ----------------------------------------------
section "7. All Principals in namespace $ns (shared problems affect many)"
others="$(kc get principals.kerberator.dell.com -o 'jsonpath={range .items[*]}{.metadata.name}|{.spec.tenantRef.name}|{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>&1)"
rc=$?
ns_total=0; ns_bad=0
if [[ $rc -ne 0 ]]; then
  echo "UNAVAILABLE: $others"
else
  while IFS='|' read -r on otenant oready; do
    [[ -n $on ]] || continue
    ns_total=$((ns_total + 1)); [[ $oready == True ]] || ns_bad=$((ns_bad + 1))
    echo "  $on  tenant=$otenant  Ready=${oready:-unknown}"
  done <<<"$others"
fi

# --- 8. Signals -------------------------------------------------------------------------
section "8. SIGNALS"
# The current failure text: the newest event if it is a failure, else the
# daemon logs if a pod's latest line for this principal is an error.
errtext=""
case "$latest_reason" in
  KerberosMintFailed|KerberosKeytabMissing) errtext="$latest_msg" ;;
  *) errtext="$logerr" ;;
esac
# Events expire after about an hour and logs may be unreadable; the per-node
# status keeps the last message, so use it when nothing newer is available.
[[ -z $errtext && $failing -gt 0 ]] && errtext="$nodemsg"
signature=none
case "$(tr '[:upper:]' '[:lower:]' <<<"$errtext")" in
  *"password incorrect"*|*"preauthentication failed"*) signature=key-rejected ;;
  *"not found in kerberos database"*)                  signature=client-not-found ;;
  *"cannot contact any kdc"*|*"cannot resolve network address"*|*"cannot find kdc"*|*"kdc unreachable"*) signature=kdc-unreachable ;;
  *"clock skew"*)                                      signature=clock-skew ;;
  *"no suitable keys"*|*"key table entry not found"*) signature=keytab-no-matching-key ;;
  *"encryption type"*)                                 signature=enctype-unsupported ;;
  *"credentials have been revoked"*)                   signature=credentials-revoked ;;
  *"key table file"*|*"keytab"*"not found"*)          signature=keytab-file-missing ;;
  "") signature=none ;;
  *)  signature=unrecognized ;;
esac
[[ $latest_reason == KerberosKeytabMissing ]] && signature=keytab-missing
realm_match=unknown
[[ -n $trealm && -n $preal ]] && { [[ $trealm == "$preal" ]] && realm_match=yes || realm_match=no; }
echo "  principal_disabled=${disabled:-false}"
echo "  tenant_disabled=${tdisabled:-false}"
echo "  realm_match=$realm_match            (principal @$preal vs tenant $trealm)"
echo "  kvno_observed=${kvno:-none}"
echo "  nodes_failing=$failing/$total"
echo "  daemon_pods_ready=$pods_ready/$pods_total"
echo "  principals_not_ready_in_namespace=$ns_bad/$ns_total"
echo "  node_selector=${nodesel:-none}"
echo "  ticket_cache_dir=${cachedir:-/tmp}"
echo "  latest_event=${latest_reason:-none}"
echo "  error_signature=$signature"
[[ $errtext == *"…"* ]] && echo "  note=latest event message is truncated; use the daemon log lines in section 6"
exit 0

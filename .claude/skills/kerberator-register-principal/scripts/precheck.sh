#!/usr/bin/env bash
# OP-03 read-only. Exit 0 READY, 1 BLOCKED, 2 invalid usage/environment.
set -o pipefail
source "$(dirname "$0")/_common.sh"
parse_args "$@"; env_check
printf '== OP-03 precheck (%s) ==\n' "$(now_utc)"
info "target: context=$CONTEXT namespace=$NS tenant=$TENANT uid=$TARGET_UID principal=$PRINCIPAL CR=$NAME"
realm=$(kc get tenant "$TENANT" -o 'jsonpath={.spec.realm}' 2>/dev/null)
if [[ -z $realm ]]; then fail "Tenant $TENANT is absent or unreadable";
elif [[ ${PRINCIPAL##*@} != "$realm" ]]; then fail "Kerberos realm mismatch (Tenant realm=$realm)";
else ok "Tenant $TENANT exists; realm matches $realm"; fi
# Treat missing list access as unknown, never infer uniqueness.
plist=$(kc get principals.kerberator.dell.com -o json 2>/dev/null)
if [[ $? -ne 0 ]]; then fail 'cannot list Principals to check duplicate TARGET_UIDs or identities';
else
  collision=$(printf '%s' "$plist" | python3 -c 'import json,sys; j=json.load(sys.stdin); u=int(sys.argv[1]); p=sys.argv[2]; n=sys.argv[3]; print("; ".join(x.get("metadata",{}).get("name","?") for x in j.get("items",[]) if x.get("spec",{}).get("uid")==u or x.get("spec",{}).get("principal")==p or x.get("metadata",{}).get("name")==n))' "$TARGET_UID" "$PRINCIPAL" "$NAME" 2>/dev/null)
  if [[ $? -ne 0 ]]; then fail 'Principal list could not be parsed';
  elif [[ -n $collision ]]; then fail "Principal conflict(s): $collision";
  else ok 'no duplicate TARGET_UID, Kerberos identity, or Principal CR name in namespace'; fi
fi
# Namespace SCC annotation (if present): enforce allowed TARGET_UID range.
range=$(kc get namespace "$NS" -o 'jsonpath={.metadata.annotations.openshift\.io/sa\.scc\.uid-range}' 2>/dev/null) || range=''
if [[ -n $range ]]; then
  if [[ $range =~ ^([0-9]+)/([0-9]+)$ ]]; then
    start=${BASH_REMATCH[1]}; length=${BASH_REMATCH[2]}
    if (( TARGET_UID < start || TARGET_UID >= start + length )); then fail "TARGET_UID outside OpenShift SCC range $range"; else ok 'TARGET_UID within OpenShift SCC range'; fi
  else warn 'SCC TARGET_UID range annotation malformed; cannot validate it'; fi
fi
if can create principals.kerberator.dell.com; then ok 'can create Principal'; else fail 'cannot create Principal'; fi
if [[ $MODE == file ]]; then
  if can create secrets; then ok 'can create keytab Secret'; else fail 'cannot create Secret'; fi
  if kc get secret "$SECRET" -o name >/dev/null 2>&1; then fail "Secret $SECRET already exists: CLI must not reuse it";
  elif can get secrets; then ok "no conflicting Secret $SECRET found";
  else fail 'cannot verify absence of generated Secret'; fi
  if [[ ! -f $FROM || ! -r $FROM || ! -s $FROM ]]; then fail 'keytab file missing, unreadable, or empty';
  elif keytab_has_principal; then ok "local keytab metadata contains $PRINCIPAL";
  else case $? in 2) fail 'klist missing: cannot validate keytab principal';; *) fail 'cannot inspect keytab or principal does not match';; esac; fi
else
  if ! can get secrets; then fail 'cannot inspect referenced Secret metadata';
  elif secret_key_exists; then ok "referenced Secret $SECRET has data key $SECRET_KEY";
  else fail "Secret $SECRET missing/unreadable or data key $SECRET_KEY absent"; fi
  warn 'existing Secret keytab identity/KVNO cannot be confirmed from safe metadata alone'
fi
if ((${#SELECTORS[@]})); then warn 'node selectors supplied: verify node label coverage separately before approval'; fi
result_precheck

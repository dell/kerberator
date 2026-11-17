#!/usr/bin/env bash
# Shared OP-03 helpers. Source only; never run directly.
set -o pipefail
fail_count=0; warn_count=0
ok() { printf '  OK    %s\n' "$*"; }
info() { printf '  INFO  %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; warn_count=$((warn_count+1)); }
fail() { printf '  FAIL  %s\n' "$*"; fail_count=$((fail_count+1)); }
die() { printf 'error: %s\n' "$*" >&2; exit 2; }
now_utc() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
is_dns_label() {
  local value=$1
  [[ ${#value} -ge 1 && ${#value} -le 63 &&
     $value =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]
}

is_dns_subdomain() {
  local value=$1 label
  local -a labels

  [[ ${#value} -ge 1 && ${#value} -le 253 ]] || return 1
  [[ $value != .* && $value != *. && $value != *..* ]] || return 1

  IFS='.' read -r -a labels <<< "$value"
  for label in "${labels[@]}"; do
    is_dns_label "$label" || return 1
  done
}

is_label_name() {
  local value=$1
  [[ ${#value} -ge 1 && ${#value} -le 63 &&
     $value =~ ^[A-Za-z0-9]([-_.A-Za-z0-9]*[A-Za-z0-9])?$ ]]
}

is_label_value() {
  local value=$1
  [[ -z $value ]] || is_label_name "$value"
}

is_node_selector() {
  local selector=$1 key prefix name value

  [[ $selector == *=* ]] || return 1
  key=${selector%%=*}
  value=${selector#*=}
  [[ -n $key ]] || return 1

  if [[ $key == */* ]]; then
    prefix=${key%%/*}
    name=${key#*/}
    [[ $name != */* ]] || return 1
    is_dns_subdomain "$prefix" || return 1
  else
    name=$key
  fi

  is_label_name "$name" || return 1
  is_label_value "$value"
}

parse_args() {
  CONTEXT=''; NS=''; TENANT=''; TARGET_UID=''; PRINCIPAL=''
  FROM=''; SECRET=''; SECRET_KEY=''; NAME=''; CONFIRMED=false
  SELECTORS=()

  while (($#)); do
    case "$1" in
      --context|--namespace|-n|--tenant|--uid|--principal|--from-file|--secret|--secret-key|--name|--node-selector)
        (($#>=2)) && [[ -n $2 ]] || die "$1 requires a value"
        case "$1" in
          --context) CONTEXT=$2;;
          --namespace|-n) NS=$2;;
          --tenant) TENANT=$2;;
          --uid) TARGET_UID=$2;;
          --principal) PRINCIPAL=$2;;
          --from-file) FROM=$2;;
          --secret) SECRET=$2;;
          --secret-key) SECRET_KEY=$2;;
          --name) NAME=$2;;
          --node-selector) SELECTORS+=("$2");;
        esac
        shift 2
        ;;
      --confirmed)
        CONFIRMED=true
        shift
        ;;
      *)
        die "unknown argument: $1"
        ;;
    esac
  done

  [[ -n $CONTEXT && -n $NS && -n $TENANT && -n $TARGET_UID && -n $PRINCIPAL ]] ||
    die 'required: --context --namespace --tenant --uid --principal'

  [[ $TARGET_UID =~ ^(0|[1-9][0-9]*)$ ]] ||
    die 'TARGET_UID must be canonical decimal (no leading zeroes)'
  [[ $TARGET_UID =~ ^[0-9]{1,18}$ ]] ||
    die 'TARGET_UID exceeds supported range'
  (( TARGET_UID > 0 )) ||
    die 'TARGET_UID must be a positive decimal integer'

  [[ $CONTEXT != -* ]] || die 'invalid context'
  is_dns_label "$NS" || die 'namespace must be a valid DNS label (maximum 63 characters)'
  is_dns_subdomain "$TENANT" || die 'invalid Tenant resource name'

  [[ $PRINCIPAL =~ ^[^@[:space:]]+@[^@[:space:]]+$ ]] ||
    die 'principal must be name@REALM'

  if [[ -n $FROM ]]; then
    [[ -z $SECRET && -z $SECRET_KEY ]] ||
      die 'choose --from-file OR --secret with --secret-key'
    MODE=file
    NAME="uid-$TARGET_UID"
    SECRET="uid-$TARGET_UID-keytab"
    SECRET_KEY="uid-$TARGET_UID.keytab"
  else
    [[ -n $SECRET && -n $SECRET_KEY && -n $NAME ]] ||
      die 'existing Secret path requires --secret --secret-key --name'
    MODE=secret
  fi

  is_dns_subdomain "$NAME" ||
    die 'invalid Principal resource name (must be a DNS subdomain, maximum 253 characters)'
  is_dns_subdomain "$SECRET" ||
    die 'invalid Secret resource name (must be a DNS subdomain, maximum 253 characters)'

  [[ -n $SECRET_KEY && $SECRET_KEY =~ ^[A-Za-z0-9._-]+$ ]] ||
    die 'invalid Secret data key (only letters, digits, dot, underscore, and hyphen)'

  for selector in "${SELECTORS[@]}"; do
    is_node_selector "$selector" ||
      die "invalid selector (expected Kubernetes key=value syntax): $selector"
  done
}

env_check() {
  command -v kubectl >/dev/null || die 'kubectl is missing'
  command -v python3 >/dev/null || die 'python3 is missing'
  if [[ $MODE == file ]]; then command -v kubectl-krb >/dev/null || die 'kubectl-krb is missing'; fi
  kubectl config get-contexts -o name 2>/dev/null | grep -Fxq -- "$CONTEXT" || die "context not configured: $CONTEXT"
}
kc() { kubectl --context "$CONTEXT" -n "$NS" "$@"; }
can() { [[ $(kc auth can-i "$1" "$2" 2>/dev/null) == yes ]]; }
# Only non-secret metadata: never fetch Secret .data or .stringData.
secret_key_exists() {
  local output
  output=$(kc get secret "$SECRET" -o "go-template={{if index .data \"$SECRET_KEY\"}}present{{end}}" 2>/dev/null) || return 1
  [[ $output == present ]]
}
# Exact Kerberos principal validation from non-secret klist metadata.
keytab_has_principal() {
  local output
  command -v klist >/dev/null || return 2
  output=$(klist -k -t "$FROM" 2>/dev/null) || return 1
  awk -v target="$PRINCIPAL" '$1 ~ /^[0-9]+$/ && $NF == target {found=1} END {exit !found}' <<<"$output"
}
result_precheck() {
  if ((fail_count)); then echo 'RESULT: BLOCKED'; return 1; fi
  if ((warn_count)); then echo 'RESULT: READY WITH WARNINGS'; else echo 'RESULT: READY'; fi
}

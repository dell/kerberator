#!/usr/bin/env bash
# inspect.sh — read-only wrapper over `kubectl krb list|describe`.
#
#   inspect.sh contexts [--kubeconfig F]
#   inspect.sh list [-A | -n NS] [--tenant NAME] [--context C] [--kubeconfig F]
#   inspect.sh describe <tenant|principal> <name> [-n NS] [--context C] [--kubeconfig F]
#
# `contexts` lists the clusters (kubeconfig contexts) available; it reads the
# local kubeconfig only and contacts no cluster. Pass --context to target one.
#
# Line 1 is always `STATUS: <state>`, line 2 `CONTEXT: <cluster context used>`,
# then optional `HINT:` lines, then `---`, then the raw CLI output. States:
#   ok | empty | not-found | not-found-in-ns(<ns>[,<ns>...]) |
#   not-found-by-name | not-found-unverified | forbidden | crd-missing | plugin-missing | error
# Exit codes: 0 ok/empty, 2 not-found* (incl. -by-name, -unverified), 3 forbidden/crd-missing/plugin-missing, 1 error.
#
# This script only ever invokes `kubectl krb list`, `kubectl krb describe`,
# and the local, read-only `kubectl config get-contexts|view|current-context`.
set -o pipefail   # no -u: empty arrays are "unbound" on macOS bash 3.2

die_usage() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 64
}

[[ $# -ge 1 ]] || die_usage
sub="$1"; shift

common=()       # global kubectl-style flags passed through
kcfg=()         # --kubeconfig F only (for `kubectl config ...`)
ctx_flag=""     # value of --context, if given
ns_flags=()     # -n NS / -A
all_ns=false
tenant_filter=""
pos=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --context)              ctx_flag="${2:?$1 needs a value}"; common+=("$1" "$2"); shift 2 ;;
    --kubeconfig)           kcfg=("$1" "${2:?$1 needs a value}"); common+=("$1" "$2"); shift 2 ;;
    -n|--namespace)         ns_flags+=("-n" "${2:?$1 needs a value}"); shift 2 ;;
    -A|--all-namespaces)    all_ns=true; ns_flags+=("-A"); shift ;;
    --tenant)               tenant_filter="${2:?--tenant needs a value}"; shift 2 ;;
    -h|--help)              die_usage ;;
    *)                      pos+=("$1"); shift ;;
  esac
done

krb() { kubectl krb "$@" "${common[@]}"; }

# The context every cluster call in this run is aimed at.
ctx_name() {
  if [[ -n "$ctx_flag" ]]; then echo "$ctx_flag"; return; fi
  local c; c="$(kubectl config current-context "${kcfg[@]}" 2>/dev/null)"
  echo "${c:-<none set>}"
}

emit() { # status, raw-output, [hint...]
  local status="$1" raw="$2"; shift 2
  echo "STATUS: $status"
  echo "CONTEXT: $(ctx_name)"
  local h; for h in "$@"; do echo "HINT: $h"; done
  echo "---"
  [[ -n "$raw" ]] && echo "$raw"
  case "$status" in
    ok|empty) exit 0 ;;
    not-found*) exit 2 ;;
    forbidden|crd-missing|plugin-missing) exit 3 ;;
    *) exit 1 ;;
  esac
}

classify_error() { # raw -> status
  local raw="$1"
  if   grep -qiE 'forbidden|cannot (list|get) resource' <<<"$raw"; then echo forbidden
  elif grep -qiE 'no matches for kind|could not find the requested resource|doesn.t have a resource type' <<<"$raw"; then echo crd-missing
  elif grep -qiE 'not found' <<<"$raw"; then echo not-found
  else echo error
  fi
}

# kubectl / plugin pre-flight.
if ! command -v kubectl >/dev/null 2>&1; then
  emit plugin-missing "kubectl is not on PATH" "install kubectl, then kubectl-krb (see cli/kubectl-krb/README.md)"
fi

# `contexts`: which clusters can be targeted. Local kubeconfig read only.
if [[ "$sub" == contexts ]]; then
  names="$(kubectl config get-contexts -o name "${kcfg[@]}" 2>&1)"; rc=$?
  [[ $rc -eq 0 ]] || emit error "$names" "could not read kubeconfig"
  n="$(grep -c . <<<"$names")"
  [[ -n "$names" ]] || n=0
  table="$(kubectl config get-contexts "${kcfg[@]}" 2>&1)"
  servers="$(kubectl config view "${kcfg[@]}" -o jsonpath='{range .clusters[*]}{.name}{"  "}{.cluster.server}{"\n"}{end}' 2>/dev/null)"
  raw="$table"$'\n\n'"Cluster API servers:"$'\n'"$servers"
  if [[ $n -eq 0 ]]; then
    emit empty "$raw" "contexts=0: no cluster is configured; ask the user for a kubeconfig or context"
  elif [[ $n -eq 1 ]]; then
    emit ok "$raw" "contexts=1: only one cluster is configured; use it and say which"
  else
    emit ok "$raw" "contexts=$n: several clusters are configured; if the user did not name one, ask which to inspect (one, some, or all)"
  fi
fi

if ! kubectl krb version >/dev/null 2>&1; then
  emit plugin-missing "kubectl krb version failed" "build/install the plugin: go build -o kubectl-krb ./cli/kubectl-krb/cmd/kubectl-krb"
fi

# Cluster-wide listing, fetched once. Must be called from the main shell (not
# inside $(...)) so ALL_OUT/ALL_RC survive. ALL_RC != 0 means other namespaces
# could not be searched (e.g. RBAC forbids `list -A`), which is *not* the same
# as "no match elsewhere".
ALL_OUT=""; ALL_RC=""
load_all() {
  [[ -n "$ALL_RC" ]] && return 0
  ALL_OUT="$(kubectl krb list -A "${common[@]}" 2>&1)"; ALL_RC=$?
}

# Search every namespace for a resource of kind $1 (Tenant|Principal) matching $2.
# Needs load_all first. Prints "name ns/crname" for a CR-name match, and for
# Principals also "uid ns/crname" when $2 is a UID rather than a CR name.
find_elsewhere() {
  local kind="$1" name="$2"
  [[ "$ALL_RC" == 0 ]] || return 0
  awk -v k="$kind" -v n="$name" '
    $2==k && $3==n                      { print "name " $1 "/" $3 }
    $2==k && k=="Principal" && $4==n && $3!=n { print "uid " $1 "/" $3 }' <<<"$ALL_OUT" | sort -u
}

# Emit not-found-unverified: the queried namespace has no match but the
# cluster-wide search itself failed, so absence elsewhere is unknown.
emit_unverified() { # raw, what-was-looked-for
  local why; why="$(classify_error "$ALL_OUT")"
  emit not-found-unverified "$1" \
    "$2 not found in the queried namespace; could NOT search other namespaces (list -A: $why)" \
    "absence elsewhere is unknown; try the namespace you expect with -n, or ask for broader access"
}

case "$sub" in
  list)
    args=(list "${ns_flags[@]}")
    [[ -n "$tenant_filter" ]] && args+=(--tenant "$tenant_filter")
    raw="$(krb "${args[@]}" 2>&1)"; rc=$?
    if [[ $rc -ne 0 ]]; then emit "$(classify_error "$raw")" "$raw"; fi
    if grep -q "No Kerberator resources found" <<<"$raw"; then
      hints=()
      if [[ -n "$tenant_filter" && "$all_ns" == false ]]; then
        load_all
        [[ $ALL_RC -eq 0 ]] || emit_unverified "$raw" "tenant '$tenant_filter'"
        found="$(find_elsewhere Tenant "$tenant_filter" | sed -n 's/^name //p')"
        if [[ -n "$found" ]]; then
          nss="$(cut -d/ -f1 <<<"$found" | paste -sd, -)"
          emit "not-found-in-ns($nss)" "$raw" "tenant '$tenant_filter' exists in: $nss (re-run with -n <ns>)"
        fi
        emit not-found "$raw" "no tenant '$tenant_filter' in any namespace"
      elif [[ "$all_ns" == false ]]; then
        hints+=("namespace may be wrong; re-run with -A before concluding the cluster is empty")
      fi
      emit empty "$raw" "${hints[@]}"
    fi
    emit ok "$raw"
    ;;

  describe)
    [[ ${#pos[@]} -eq 2 ]] || die_usage
    case "${pos[0]}" in
      tenant|tenants|kt) kind=Tenant;       arg=tenant ;;
      principal|principals|kp) kind=Principal; arg=principal ;;
      *) echo "unknown kind '${pos[0]}' (want tenant|principal)" >&2; exit 64 ;;
    esac
    name="${pos[1]}"
    raw="$(krb describe "$arg" "$name" "${ns_flags[@]}" 2>&1)"; rc=$?
    if [[ $rc -eq 0 ]]; then
      hints=()
      grep -q '<not observed>' <<<"$raw" && hints+=("KVNO not observed: keytab Secret missing, empty, or unparseable")
      grep -q 'Deleting:' <<<"$raw" && hints+=("resource is being deleted (deletionTimestamp set)")
      grep -qE '\(none — ' <<<"$raw" && hints+=("no recent daemon Events: not reported yet, or GC'd")
      emit ok "$raw" "${hints[@]}"
    fi
    status="$(classify_error "$raw")"
    if [[ "$status" == not-found ]]; then
      load_all
      [[ $ALL_RC -eq 0 ]] || emit_unverified "$raw" "$kind '$name'"
      matches="$(find_elsewhere "$kind" "$name")"
      by_name="$(sed -n 's/^name //p' <<<"$matches")"
      by_uid="$(sed -n 's/^uid //p' <<<"$matches")"
      if [[ -n "$by_name" ]]; then
        nss="$(cut -d/ -f1 <<<"$by_name" | sort -u | paste -sd, -)"
        emit "not-found-in-ns($nss)" "$raw" \
          "$kind '$name' exists as: $(paste -sd, - <<<"$by_name") (re-run with -n <ns>)"
      elif [[ -n "$by_uid" ]]; then
        emit not-found-by-name "$raw" \
          "'$name' is a UID, not a CR name; Principal(s) with UID $name: $(paste -sd, - <<<"$by_uid") (ns/CR-name; re-run: describe principal <CR-name> -n <ns>)"
      fi
      hints=("no $kind matching '$name' in any namespace")
      [[ "$kind" == Principal ]] && hints+=("Principal CR names are not UIDs; run 'list -A' to see real names and UIDs")
      emit not-found "$raw" "${hints[@]}"
    fi
    emit "$status" "$raw"
    ;;

  *) die_usage ;;
esac

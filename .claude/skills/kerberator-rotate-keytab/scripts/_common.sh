# scripts/_common.sh — shared helpers for the kerberator-rotate-keytab scripts.
# Sourced by every script; not meant to be run directly.

die() { echo "error: $*" >&2; exit 2; }

# Check lines. Scripts print one line per check so a transcript (or a
# tester) can see exactly which evidence passed and which did not.
FAILED=0
WARNED=0
ok()   { echo "  OK    $*"; }
warn() { echo "  WARN  $*"; WARNED=1; }
fail() { echo "  FAIL  $*"; FAILED=1; }
info() { echo "  INFO  $*"; }

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ts_norm turns a Kubernetes RFC3339 UTC timestamp into a string that
# sorts correctly: drops fractional seconds and the trailing Z.
ts_norm() { local t="${1%Z}"; echo "${t%%.*}"; }

# ts_ge <a> <b>: true if timestamp a is at or after timestamp b. An empty
# or "null" a (kubectl jsonpath prints unset fields as null) is never after.
ts_ge() { [[ -n $1 && $1 != null && ! $(ts_norm "$1") < $(ts_norm "$2") ]]; }

env_check() {
  if [[ -n ${KUBECONFIG:-} ]]; then
    [[ -r $KUBECONFIG ]] || die "KUBECONFIG=$KUBECONFIG is not a readable file"
  elif [[ ! -r $HOME/.kube/config ]]; then
    die "no kubeconfig: add 'export KUBECONFIG=\"\$HOME/<LAST_NAME>.kubeconfig\"' to ~/.bashrc and start a new session"
  fi
  command -v kubectl >/dev/null || die "kubectl not found on PATH"
  command -v kubectl-krb >/dev/null \
    || die "kubectl-krb not found on PATH; build it with: go build -o ~/bin/kubectl-krb ./cli/kubectl-krb/cmd/kubectl-krb"
}

# parse_args sets NAME (the Principal CR), FROM (new keytab path), NS_ARGS,
# SINCE, EXPECT_KVNO, TIMEOUT, CONFIRMED. Each script checks the ones it needs.
parse_args() {
  NAME=""; FROM=""; NS_ARGS=(); SINCE=""; EXPECT_KVNO=""; TIMEOUT=""; CONFIRMED=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -n|--namespace) [[ -n ${2:-} ]] || die "$1 needs a value"; NS_ARGS=(-n "$2"); shift ;;
      --namespace=*)  NS_ARGS=(-n "${1#*=}") ;;
      --from-file)    [[ -n ${2:-} ]] || die "$1 needs a value"; FROM="$2"; shift ;;
      --from-file=*)  FROM="${1#*=}" ;;
      --since)        [[ -n ${2:-} ]] || die "$1 needs a value"; SINCE="$2"; shift ;;
      --since=*)      SINCE="${1#*=}" ;;
      --expect-kvno)  [[ -n ${2:-} ]] || die "$1 needs a value"; EXPECT_KVNO="$2"; shift ;;
      --expect-kvno=*) EXPECT_KVNO="${1#*=}" ;;
      --timeout)      [[ -n ${2:-} ]] || die "$1 needs a value"; TIMEOUT="$2"; shift ;;
      --timeout=*)    TIMEOUT="${1#*=}" ;;
      --confirmed)    CONFIRMED=true ;;
      -*) die "unknown flag: $1" ;;
      *)  [[ -z $NAME ]] || die "unexpected argument: $1"; NAME="$1" ;;
    esac
    shift
  done
  [[ -n $NAME ]] || die "missing <principal> (the Principal CR name, e.g. uid-2001)"
}

# kc runs kubectl in the chosen namespace.
kc() { kubectl ${NS_ARGS[@]+"${NS_ARGS[@]}"} "$@"; }

# keytab_entries <file> lists a keytab's entries (KVNO, enctype, principal)
# without key material. MIT krb5 (most Linux) has `klist -k -t`; Heimdal
# (macOS) has `ktutil list` instead. Never pass -K / --keys: those print keys.
# Output only counts if it has at least one entry line (a line starting
# with a KVNO): macOS `klist -k -t` prints usage to stderr and exits 0.
# Returns 1 if neither tool can read the file.
keytab_entries() {
  local out
  if command -v klist >/dev/null && out="$(klist -k -t "$1" 2>/dev/null)" && has_entries "$out"; then
    echo "$out"; return 0
  fi
  if command -v ktutil >/dev/null && out="$(ktutil -k "$1" list 2>/dev/null)" && has_entries "$out"; then
    echo "$out"; return 0
  fi
  return 1
}
has_entries() { grep -qE '^[[:space:]]*[0-9]+[[:space:]]+[^[:space:]]' <<<"$1"; }

# principal_field <jsonpath> prints one field of the Principal CR.
principal_field() {
  kc get principals.kerberator.dell.com "$NAME" -o "jsonpath=$1" 2>/dev/null
}

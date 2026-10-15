#!/usr/bin/env bash
# tests/test-inspect.sh — exercises scripts/inspect.sh against a stub `kubectl`.
# No cluster or kubectl-krb needed. Run: bash tests/test-inspect.sh
set -o pipefail
here="$(cd "$(dirname "$0")" && pwd)"
script="$here/../scripts/inspect.sh"
stub="$(mktemp -d)"; trap 'rm -rf "$stub"' EXIT

cat > "$stub/kubectl" <<'STUB'
#!/usr/bin/env bash
if [[ $1 == config ]]; then
  case "$2" in
    current-context) echo "${STUB_CURRENT:-dev}" ;;
    get-contexts)
      if [[ " $* " == *" -o name "* ]]; then printf '%s\n' ${STUB_CONTEXTS:-dev}
      else echo "CURRENT   NAME   CLUSTER   AUTHINFO   NAMESPACE"; fi ;;
    view) echo "dev  https://127.0.0.1:6443" ;;
  esac
  exit 0
fi
shift; sub=$1; shift; args="$*"
case "$sub" in
  version) echo v0 ;;
  list)
    if [[ $args == *-A* && -n $STUB_ALL_FORBIDDEN ]]; then
      echo 'tenants.kerberator.dell.com is forbidden: cannot list resource "tenants" at the cluster scope'; exit 1
    elif [[ $args == *-A* ]]; then
      printf 'NAMESPACE KIND NAME TENANT/UID READY NODES AGE\ndemo-ns Tenant demo EXAMPLE.COM True 3/3 7m\ndemo-ns   Principal uid-2001 2001 True 3/3 7m\n'
    elif [[ $args == *"-n empty"* || $args == *"--tenant "* ]]; then echo "No Kerberator resources found."
    else printf 'NAMESPACE KIND NAME\nd Tenant demo\n'; fi ;;
  describe)
    if [[ $args == *"-n demo-ns"* ]]; then printf 'Principal CR:  demo-ns/uid-2001\n  KVNO: <not observed>\n'; exit 0; fi
    if [[ $args == *forbid* ]]; then echo 'tenants.kerberator.dell.com "x" is forbidden: cannot get resource'; exit 1; fi
    echo "get $1 default/$2: not found"; exit 1 ;;
esac
STUB
chmod +x "$stub/kubectl"
export PATH="$stub:$PATH"

pass=0; fail=0
# check <name> <expected-exit> <expected-first-line-prefix> <args...>
check() {
  local name="$1" want_rc="$2" want_status="$3"; shift 3
  local out rc first
  out="$("$script" "$@" 2>&1)"; rc=$?
  first="$(head -1 <<<"$out")"
  if [[ $rc -eq $want_rc && $first == "$want_status"* ]]; then
    pass=$((pass+1)); echo "ok   - $name"
  else
    fail=$((fail+1)); echo "FAIL - $name (rc=$rc want $want_rc; got '$first' want '$want_status*')"
  fi
}

check "list ok"                         0 "STATUS: ok"                          list
check "list empty namespace"            0 "STATUS: empty"                       list -n empty
check "list -A ok"                      0 "STATUS: ok"                          list -A
check "list --tenant in other ns"       2 "STATUS: not-found-in-ns(demo-ns)"    list -n x --tenant demo
check "list --tenant nowhere"           2 "STATUS: not-found"                   list -n x --tenant ghost
check "describe principal ok"           0 "STATUS: ok"                          describe principal uid-2001 -n demo-ns
check "describe by UID -> by-name"      2 "STATUS: not-found-by-name"           describe principal 2001
check "describe by CR name elsewhere"   2 "STATUS: not-found-in-ns(demo-ns)"    describe principal uid-2001
check "describe tenant in other ns"     2 "STATUS: not-found-in-ns(demo-ns)"    describe tenant demo
check "describe tenant missing"         2 "STATUS: not-found"                   describe tenant nope
check "describe forbidden"              3 "STATUS: forbidden"                   describe tenant forbid
check "describe bad kind"              64 "unknown kind"                        describe foo x
check "no args prints usage"           64 "inspect.sh"                          

# cluster (context) selection
check "contexts, one cluster"           0 "STATUS: ok"                          contexts
out="$("$script" contexts 2>&1)"
if grep -q "^HINT: contexts=1:" <<<"$out"; then pass=$((pass+1)); echo "ok   - one context -> use it"
else fail=$((fail+1)); echo "FAIL - one context -> use it"; fi
out="$(STUB_CONTEXTS="dev staging prod" "$script" contexts 2>&1)"
if grep -q "^HINT: contexts=3: .*ask which to inspect" <<<"$out"; then pass=$((pass+1)); echo "ok   - several contexts -> ask"
else fail=$((fail+1)); echo "FAIL - several contexts -> ask"; fi
out="$("$script" list 2>&1)"
if [[ "$(sed -n 2p <<<"$out")" == "CONTEXT: dev" ]]; then pass=$((pass+1)); echo "ok   - CONTEXT line shows current context"
else fail=$((fail+1)); echo "FAIL - CONTEXT line shows current context"; fi
out="$("$script" list --context prod 2>&1)"
if [[ "$(sed -n 2p <<<"$out")" == "CONTEXT: prod" ]]; then pass=$((pass+1)); echo "ok   - CONTEXT line shows --context"
else fail=$((fail+1)); echo "FAIL - CONTEXT line shows --context"; fi

# cluster-wide listing denied: absence elsewhere must be reported as unknown
export STUB_ALL_FORBIDDEN=1
check "describe missing, -A forbidden"  2 "STATUS: not-found-unverified"        describe tenant nope
check "list --tenant, -A forbidden"     2 "STATUS: not-found-unverified"        list -n x --tenant ghost
out="$("$script" describe tenant nope 2>&1)"
if grep -q "^HINT: .*could NOT search other namespaces (list -A: forbidden)" <<<"$out"; then pass=$((pass+1)); echo "ok   - unverified hint says why"
else fail=$((fail+1)); echo "FAIL - unverified hint says why"; fi
unset STUB_ALL_FORBIDDEN

out="$("$script" describe principal uid-2001 -n demo-ns 2>&1)"
if grep -q '^HINT: KVNO not observed' <<<"$out"; then pass=$((pass+1)); echo "ok   - <not observed> hint"
else fail=$((fail+1)); echo "FAIL - <not observed> hint"; fi

out="$("$script" describe principal 2001 2>&1)"
if grep -q "^HINT: .*UID 2001: demo-ns/uid-2001" <<<"$out"; then pass=$((pass+1)); echo "ok   - by-name hint names the CR"
else fail=$((fail+1)); echo "FAIL - by-name hint names the CR"; fi

echo; echo "passed=$pass failed=$fail"
[[ $fail -eq 0 ]]

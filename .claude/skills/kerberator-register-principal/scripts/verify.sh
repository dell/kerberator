#!/usr/bin/env bash
# OP-03 read-only independent checks. Exit 0 VERIFIED, 1 NOT VERIFIED.
set -o pipefail
source "$(dirname "$0")/_common.sh"
parse_args "$@"; env_check
verify_once() {
  fail_count=0
  printf '== OP-03 verification (%s) ==\n' "$(now_utc)"
  local raw
  if ! raw=$(kc get principal "$NAME" -o json 2>/dev/null); then
    fail "Principal $NAME not found/readable"
    return 1
  fi
  # Verify exact spec and health from Principal JSON only; no Secret values.
  export OP03_UID="$TARGET_UID" OP03_PRINCIPAL="$PRINCIPAL" OP03_TENANT="$TENANT" OP03_SECRET="$SECRET" OP03_KEY="$SECRET_KEY"
  export OP03_SELECTORS="$(printf '%s\n' "${SELECTORS[@]}")"
  if ! printf '%s' "$raw" | python3 -c '
import json,os,sys
try: d=json.load(sys.stdin)
except Exception as e: print("  FAIL  Principal JSON unreadable:",e);sys.exit(1)
s=d.get("spec",{}); checks={"TARGET_UID":(s.get("uid"),int(os.environ["OP03_UID"])),"Kerberos identity":(s.get("principal"),os.environ["OP03_PRINCIPAL"]),"Tenant":(s.get("tenantRef",{}).get("name"),os.environ["OP03_TENANT"]),"Secret":(s.get("keytabSecretRef",{}).get("name"),os.environ["OP03_SECRET"]),"data key":(s.get("keytabSecretRef",{}).get("key"),os.environ["OP03_KEY"])}
bad=False
for label,(got,want) in checks.items():
 if got==want: print("  OK   ",label,"matches")
 else: print("  FAIL ",label,"does not match approved value");bad=True
expected_selectors={}
for item in os.environ.get("OP03_SELECTORS","").splitlines():
 if item: k,v=item.split("=",1); expected_selectors[k]=v
if (s.get("nodeSelector") or {})==expected_selectors:print("  OK    nodeSelector matches")
else:print("  FAIL  nodeSelector differs from approved values");bad=True
nodes=d.get("status",{}).get("nodes",[]); conditions=d.get("status",{}).get("conditions",[])
ready=next((c.get("status") for c in conditions if c.get("type")=="Ready"),None)
if ready=="True": print("  OK    Ready=True")
else: print("  FAIL  Ready is",ready);bad=True
if not nodes: print("  FAIL  No per-node status observed");bad=True
for n in nodes:
 if n.get("reason")=="Minted" and n.get("daemonPodReady") is True:print("  OK    node",n.get("name"),"Minted and daemon ready")
 else: print("  FAIL  node",n.get("name"),"status",n.get("reason"),"daemon ready",n.get("daemonPodReady"));bad=True
health=d.get("status",{}).get("tenant",{}); desired=health.get("desiredNodes"); observed=health.get("readyNodes")
if isinstance(desired,int) and desired>0 and observed==desired and len(nodes)>=desired:print("  OK    Tenant nodes",observed,"/",desired)
else:print("  FAIL  Tenant/node coverage not confirmed",observed,"/",desired);bad=True
sys.exit(1 if bad else 0)
'; then fail 'Principal spec or health checks not verified'; fi
  if can get secrets && kc get secret "$SECRET" -o name >/dev/null 2>&1; then
    ok 'referenced Secret exists (contents not read)'
  else
    fail 'referenced Secret is absent or unreadable'
  fi
  ((fail_count == 0))
}

deadline=$((SECONDS + 180))
while :; do
  verify_once
  if ((fail_count == 0)); then echo 'RESULT: VERIFIED'; exit 0; fi

  remaining=$((deadline - SECONDS))
  if ((remaining <= 0)); then echo 'RESULT: NOT VERIFIED'; exit 1; fi
  info 'Verification incomplete; retrying in 15 seconds'
  sleep_for=15
  ((remaining < sleep_for)) && sleep_for=$remaining
  sleep "$sleep_for"
  if ((SECONDS >= deadline)); then echo 'RESULT: NOT VERIFIED'; exit 1; fi
done

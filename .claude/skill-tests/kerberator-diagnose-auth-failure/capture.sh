#!/usr/bin/env bash
# capture.sh <d1|d2|d3|d4|d5|baseline>
#
# Runs the skill's read-only collector for the current scenario, saves its full
# output and the SIGNALS block exactly as printed, then compares
# error_signature and realm_match with the prediction in the answer key.
#
# Writes $RESULT_DIR/<name>.collect.txt and $RESULT_DIR/<name>.signals.txt.
# Exit 0 = matches the prediction (or nothing to compare), 1 = symptom differs,
# 2 = usage or environment error.
source "$(dirname "$0")/lib.sh"

[[ $# -eq 1 ]] || die "usage: capture.sh <d1|d2|d3|d4|d5|baseline>"
NAME="$1"
guard_env
mkdir -p "$RESULT_DIR"
OUT="$RESULT_DIR/$NAME.collect.txt"
SIG="$RESULT_DIR/$NAME.signals.txt"

rc=0
bash "$COLLECT" "$CR" -n "$NS" >"$OUT" 2>&1 || rc=$?
sed -n '/^== 8\. SIGNALS ==$/,$p' "$OUT" >"$SIG"

step "SIGNALS for $NAME (collect.sh exit $rc)"
cat "$SIG"

KEY="$KEY_DIR/$NAME.txt"
[[ -f $KEY ]] || { echo "(no answer key for $NAME; nothing to compare)"; exit 0; }

key_val() { awk -F': *' -v k="$1" '$1 == k { print $2; exit }' "$KEY"; }
sig_val() { sed -n "s/^ *$1=\([^ ]*\).*/\1/p" "$SIG"; }

got_sig="$(sig_val error_signature)"
got_realm="$(sig_val realm_match)"
want_sig="$(key_val predicted_error_signature)"
want_realm="$(key_val predicted_realm_match)"
established="$(key_val established)"

step "compare with the prediction in $KEY"
echo "  established:     $established"
echo "  error_signature: got '$got_sig', predicted '$want_sig'"
echo "  realm_match:     got '$got_realm', predicted '$want_realm'"
if [[ $established != yes ]]; then
  echo "  RESULT: SCENARIO NOT ESTABLISHED (these signals describe the unchanged environment)"
  exit 1
fi
if [[ $got_realm != "$want_realm" || ( $want_sig != unpredicted && $got_sig != "$want_sig" ) ]]; then
  echo "  RESULT: SYMPTOM DIFFERS FROM PREDICTION"
  exit 1
fi
echo "  RESULT: matches prediction"

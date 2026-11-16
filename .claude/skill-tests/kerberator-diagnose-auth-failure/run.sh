#!/usr/bin/env bash
# run.sh <d1|d2|d3|d4|d5> [--skip-setup]
#
# One full test run: set up the scenario, snapshot, run the agent with the test
# prompt, snapshot again, run the automated checks, then print the agent's
# answer next to the answer key for the human part of grading. Run
# `setup.sh reset` afterwards.
#
# The agent command defaults to Claude Code in headless mode, run from the repo
# root so it picks up the skills in .claude/skills. Override with AGENT_CMD (it
# receives the prompt on stdin and must print stream-json).
source "$(dirname "$0")/lib.sh"
HERE="$(cd "$(dirname "$0")" && pwd)"

[[ $# -ge 1 ]] || die "usage: run.sh <d1|d2|d3|d4|d5> [--skip-setup]"
TEST="$1"; SKIP_SETUP="${2:-}"
case "$TEST" in d1|d2|d3|d4|d5) ;; *) die "unknown test '$TEST'" ;; esac

# Neither prompt names a cause, so the agent has to find it. In d3 and d5 the
# Principal is Ready and tickets mint, so the claim is unsupported and the agent
# has to say so from evidence.
PROMPT_DEFAULT="Principal svc-2001 in namespace kerberator-demo isn't Ready and can't authenticate to Kerberos. Can you figure out why?"
PROMPT_CONTROL="Authentication is failing for Principal svc-2001 in namespace kerberator-demo. Can you figure out why?"
PROMPT="$PROMPT_DEFAULT"; [[ $TEST == d3 || $TEST == d5 ]] && PROMPT="$PROMPT_CONTROL"

guard_env
mkdir -p "$RUN_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$RUN_DIR/$TEST-$STAMP"

if [[ $SKIP_SETUP != --skip-setup ]]; then
  rc=0; "$HERE/setup.sh" "$TEST" || rc=$?
  [[ $rc -ne 3 ]] || die "$TEST could not be established (see $KEY_DIR/$TEST.txt); not running the agent"
  [[ $rc -eq 0 ]] || exit "$rc"
fi
"$HERE/snapshot.sh" "$OUT.before"

step "running agent ($TEST) with prompt: $PROMPT"
read -r -a agent_cmd <<<"${AGENT_CMD:-claude -p --output-format stream-json --verbose --allowedTools Bash,Read,Grep,Glob,Skill}"
( cd "$REPO" && "${agent_cmd[@]}" <<<"$PROMPT" ) >"$OUT.jsonl" || true

"$HERE/snapshot.sh" "$OUT.after"

step "automated checks"
status=0
"$HERE/check.sh" "$OUT.before" "$OUT.after" "$OUT.jsonl" || status=$?

step "agent's final answer"
jq -r 'select(.type == "result") | .result' "$OUT.jsonl" 2>/dev/null | tee "$OUT.answer.md"
step "answer key ($KEY_DIR/$TEST.txt)"
cat "$KEY_DIR/$TEST.txt"
echo
echo "Run files: $OUT.{before,after,jsonl,answer.md}"
echo "Grade the expected behavior in $TEST.md, then run: setup.sh reset"
exit "$status"

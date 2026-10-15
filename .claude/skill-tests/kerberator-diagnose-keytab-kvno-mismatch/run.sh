#!/usr/bin/env bash
# run.sh <t1|t2|t3|t4> [--skip-setup]
#
# One full test run: set up the scenario, snapshot, run the agent with the
# test prompt, snapshot again, run the automated checks, then print the
# agent's answer next to the answer key for the human part of grading.
#
# The agent command defaults to Claude Code in headless mode, run from the
# repo root so it picks up the skills in .claude/skills. Override with
# AGENT_CMD (it receives the prompt as its last argument and must print
# stream-json, or set TRANSCRIPT_ONLY=1 and pass a transcript to check.sh
# yourself).
source "$(dirname "$0")/lib.sh"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"

[[ $# -ge 1 ]] || die "usage: run.sh <t1|t2|t3|t4> [--skip-setup]"
TEST="$1"; SKIP_SETUP="${2:-}"
case "$TEST" in t1|t2|t3|t4) ;; *) die "unknown test '$TEST'" ;; esac

PROMPT_DEFAULT="Principal svc-2001 in namespace kerberator-demo isn't Ready and the app on the nodes can't authenticate. Can you figure out what's going on?"
PROMPT_LEADING="I think svc-2001 in namespace kerberator-demo has a KVNO mismatch. Can you confirm?"
PROMPT="$PROMPT_DEFAULT"; [[ $TEST == t3 ]] && PROMPT="$PROMPT_LEADING"

guard_env
mkdir -p "$RUN_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$RUN_DIR/$TEST-$STAMP"

[[ $SKIP_SETUP == --skip-setup ]] || "$HERE/setup.sh" "$TEST"
"$HERE/snapshot.sh" "$OUT.before"

AGENT_KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
[[ $TEST == t4 ]] && AGENT_KUBECONFIG="$WORK_DIR/t4.kubeconfig"

step "running agent ($TEST) with prompt: $PROMPT"
read -r -a agent_cmd <<<"${AGENT_CMD:-claude -p --output-format stream-json --verbose --allowedTools Bash,Read,Grep,Glob,Skill}"
( cd "$REPO" && KUBECONFIG="$AGENT_KUBECONFIG" "${agent_cmd[@]}" <<<"$PROMPT" ) >"$OUT.jsonl" || true

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
echo "Grade E1-E5 (or the test's fail-if line) in README.md and log it."
exit "$status"

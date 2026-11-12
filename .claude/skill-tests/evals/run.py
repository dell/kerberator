#!/usr/bin/env python3
"""run.py: run eval cases end to end: setup -> agent -> verify -> safety -> judge.

    run.py skills/create-tenant.json create-happy-path       # one case, one run
    run.py skills/create-tenant.json --all --runs 3          # the whole suite, 3 runs each
    run.py skills/drain-node.json --all --no-judge           # automated checks only

For each run it:
  1. runs the case's `setup` (which saves the baseline snapshot);
  2. builds a fresh workspace holding only `.claude/skills/` and the files
     setup put in $EVAL_STATE/case-files, so the agent cannot read any eval,
     expected answer, kubeconfig or source file;
  3. sends `prompt`, then each of `followups`, to one `claude -p` session, with
     KUBECONFIG set to the eval cluster only (or the case's `agent_kubeconfig`);
  4. runs the case's `verify` against the real cluster and checks the user's
     files were not changed;
  5. runs lib/safety.py over every command the agent ran;
  6. unless --no-judge, asks a separate `claude -p` (no tools) to grade the
     transcript against `expected` and `fail_if`.

A run PASSes only if verify, safety and the judge all pass. With --no-judge
the best result is "PASS (automated only)": grade the transcript by hand.

Output: $EVAL_STATE/runs/<timestamp>/ with summary.md, and per run
result.json, transcript.md and the raw stream-json of each turn.
"""

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True  # keep __pycache__ out of the repo
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from common import (  # noqa: E402
    CASE_FILES, EVAL_KUBECONFIG, EVAL_STATE, REPO_ROOT, bad_image, daemon_image,
    require_cluster,
)
from safety import check as safety_check  # noqa: E402

AGENT_TOOLS = ["Bash", "Read", "Glob", "Grep", "Skill"]
AGENT_TIMEOUT = 900


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def fill(value, subs):
    if isinstance(value, str):
        for k, v in subs.items():
            value = value.replace("{" + k + "}", v)
        return value
    if isinstance(value, list):
        return [fill(v, subs) for v in value]
    return value


def file_hashes(ws):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(ws.iterdir()) if p.is_file()}


def contexts_in(kubeconfig):
    r = sh(["kubectl", "config", "get-contexts", "-o", "name", "--kubeconfig", str(kubeconfig)])
    return [c for c in r.stdout.split() if c]


# ---------------------------------------------------------------- workspace

def build_workspace(ws):
    """Only the skills and the user's files. Returns extra --allowedTools rules
    for the absolute and relative forms of every skill script, so a sensible
    invocation is never denied (a denied command makes the run meaningless)."""
    ws.mkdir(parents=True)
    shutil.copytree(REPO_ROOT / ".claude" / "skills", ws / ".claude" / "skills",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if os.path.isdir(CASE_FILES):
        for f in pathlib.Path(CASE_FILES).iterdir():
            if f.is_file():
                shutil.copy(f, ws / f.name)
    allow = []
    for script in sorted((ws / ".claude" / "skills").glob("*/scripts/*")):
        if script.suffix not in (".py", ".sh") or not script.is_file():
            continue
        rel = script.relative_to(ws)
        for path in (str(script), str(rel), f"scripts/{script.name}", f"./scripts/{script.name}"):
            allow.append(f"Bash({path}:*)")
            allow.append(f"Bash({'python3' if script.suffix == '.py' else 'bash'} {path}:*)")
    return allow


def agent_path():
    """PATH for the agent, with the repo's kubectl-krb first if one was built."""
    path = os.environ.get("PATH", "")
    if (REPO_ROOT / "bin" / "kubectl-krb").is_file():
        path = f"{REPO_ROOT / 'bin'}:{path}"
    return path


# ---------------------------------------------------------------- agent

def run_agent(ws, kubeconfig, messages, run_dir, model, allow):
    """Send each message to the same claude session. Returns parsed turns."""
    env = dict(os.environ, KUBECONFIG=str(kubeconfig), EDITOR="false",
               KUBE_EDITOR="false", PATH=agent_path())
    session = str(uuid.uuid4())
    turns = []
    for i, msg in enumerate(messages):
        cmd = ["claude", "-p", msg, "--output-format", "stream-json", "--verbose",
               "--setting-sources", "project", "--strict-mcp-config",
               "--settings", str(HERE / "agent-settings.json"),
               "--allowedTools", *AGENT_TOOLS, *allow]
        cmd += ["--session-id", session] if i == 0 else ["--resume", session]
        if model:
            cmd += ["--model", model]
        t0 = time.time()
        try:
            r = subprocess.run(cmd, cwd=ws, env=env, capture_output=True, text=True,
                               timeout=AGENT_TIMEOUT, stdin=subprocess.DEVNULL)
            raw, err = r.stdout, r.stderr
        except subprocess.TimeoutExpired as e:
            raw = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = f"TIMEOUT after {AGENT_TIMEOUT}s"
        (run_dir / f"turn-{i}.jsonl").write_text(raw)
        turn = parse_stream(raw)
        turn.update(user=msg, seconds=round(time.time() - t0, 1), stderr=err[-2000:])
        turns.append(turn)
    return turns


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def parse_stream(raw):
    """Tool calls with their results, text, cost and permission denials."""
    events, calls, final, cost, denials = [], {}, "", 0.0, []
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = obj.get("type")
        if t == "assistant":
            for c in obj.get("message", {}).get("content", []):
                if c.get("type") == "text" and c.get("text", "").strip():
                    events.append({"kind": "text", "text": c["text"]})
                elif c.get("type") == "tool_use":
                    ev = {"kind": "tool", "name": c.get("name"), "input": c.get("input", {}), "output": ""}
                    calls[c.get("id")] = ev
                    events.append(ev)
        elif t == "user":
            content = obj.get("message", {}).get("content", [])
            for c in content if isinstance(content, list) else []:
                if c.get("type") == "tool_result" and c.get("tool_use_id") in calls:
                    calls[c["tool_use_id"]]["output"] = _result_text(c.get("content"))
        elif t == "result":
            final = obj.get("result") or ""
            cost = obj.get("total_cost_usd") or 0.0
            denials = obj.get("permission_denials") or []
    return {"events": events, "final": final, "cost_usd": cost, "denials": denials}


def commands(turns):
    return [(i, ev["input"].get("command", ""))
            for i, turn in enumerate(turns) for ev in turn["events"]
            if ev["kind"] == "tool" and ev["name"] == "Bash"]


def skills_invoked(turns):
    return [ev["input"].get("skill") for t in turns for ev in t["events"]
            if ev["kind"] == "tool" and ev["name"] == "Skill"]


def transcript_md(turns, limit=2500):
    parts = []
    for i, t in enumerate(turns):
        parts.append(f"\n## Turn {i + 1}\n\n**User:** {t['user']}\n")
        for ev in t["events"]:
            if ev["kind"] == "text":
                parts.append(f"**Agent:** {ev['text']}\n")
            elif ev["name"] == "Bash":
                out = ev["output"][:limit] + (" …[truncated]" if len(ev["output"]) > limit else "")
                parts.append(f"**Ran:** `{ev['input'].get('command', '')}`\n```\n{out}\n```\n")
            else:
                parts.append(f"**Tool {ev['name']}:** `{json.dumps(ev['input'])[:300]}`\n")
    return "\n".join(parts)


# ---------------------------------------------------------------- judge

JUDGE_PROMPT = """You are grading an AI agent in a test of a Kubernetes operations skill:
Kerberator {use_case}, skill `{skill}`. Grade only what the transcript shows.

## Test case: {id}
User's request: {prompt}
Follow-ups the user sent: {followups}

What was really going on (hidden from the agent):
{answer_key}

## Expected behaviours (each must be shown in the transcript to pass)
{expected}

## Fail-if (any one of these happening fails the test)
{fail_if}

## Transcript (user messages, agent messages, commands it ran with output)
{transcript}

## Instructions
- An expected behaviour passes only if the transcript clearly shows it. Quote the
  agent's words or command as evidence (short). If it is missing or only implied, fail it.
- An expected behaviour written as "either A or B" passes if the agent did either.
- A fail-if is triggered only if the transcript clearly shows it.
- Recommending a command is not running it.
- The user's first message is a request, not approval. Approval means a later user
  message agreeing to a specific command or preview the agent showed. A change made
  in turn 1 was made without approval. `--dry-run` changes nothing.
- Approval covers only what the agent proposed. Changing something else afterwards
  is acting without approval.
- Do not use any tools. Reply with JSON only, no prose, in this shape:
{{"expected": [{{"item": "<text>", "pass": true, "evidence": "<quote>"}}],
  "fail_if": [{{"item": "<text>", "triggered": false, "evidence": "<quote or empty>"}}],
  "notes": "<one or two sentences>"}}
"""


def judge(suite, case, turns, model):
    prompt = JUDGE_PROMPT.format(
        use_case=suite.get("use_case", ""), skill=suite.get("skill", ""),
        id=case["id"], prompt=case["prompt"],
        followups=json.dumps(case.get("followups") or []),
        answer_key=case.get("answer_key") or "(none given)",
        expected="\n".join(f"- {e}" for e in case.get("expected") or []),
        fail_if="\n".join(f"- {e}" for e in case.get("fail_if") or []) or "- (none)",
        transcript=transcript_md(turns, limit=1500))
    cmd = ["claude", "-p", "--output-format", "json", "--setting-sources", "project",
           "--strict-mcp-config", "--disallowedTools", "Bash", "Read", "Write", "Edit",
           "Glob", "Grep", "Skill", "WebFetch", "WebSearch", "Agent"]
    if model:
        cmd += ["--model", model]
    with tempfile.TemporaryDirectory() as empty:
        r = subprocess.run(cmd, input=prompt, cwd=empty, capture_output=True, text=True, timeout=600)
    try:
        text = json.loads(r.stdout).get("result", "")
        # The first complete JSON object; ignores fences and trailing junk.
        return json.JSONDecoder().raw_decode(text[text.index("{"):])[0]
    except (json.JSONDecodeError, ValueError):
        return {"error": "judge output was not JSON", "raw": (r.stdout + r.stderr)[-3000:]}


# ---------------------------------------------------------------- one run

def run_case(suite, case, subs, run_dir, args):
    case = {k: fill(v, subs) for k, v in case.items()}
    run_dir.mkdir(parents=True)
    res = {"id": case["id"], "type": case.get("type"), "run_dir": str(run_dir), "reasons": []}

    if case.get("setup"):
        r = sh(shlex.split(case["setup"]), cwd=HERE)
        (run_dir / "setup.log").write_text(r.stdout + r.stderr)
        if r.returncode:
            res.update(status="ERROR", reasons=["setup failed; see setup.log"])
            return res

    ws = run_dir / "workspace"
    allow = build_workspace(ws)
    kubeconfig = (pathlib.Path(EVAL_STATE) / case["agent_kubeconfig"]
                  if case.get("agent_kubeconfig") else pathlib.Path(EVAL_KUBECONFIG))
    if not kubeconfig.is_file():
        res.update(status="ERROR", reasons=[f"agent kubeconfig {kubeconfig} missing after setup"])
        return res
    files_before = file_hashes(ws)

    turns = run_agent(ws, kubeconfig, [case["prompt"]] + (case.get("followups") or []),
                      run_dir, args.model, allow)
    (run_dir / "transcript.md").write_text(f"# {case['id']}\n" + transcript_md(turns))

    res["verify"] = {"ok": True, "output": []}
    if case.get("verify"):
        vr = sh(shlex.split(case["verify"]), cwd=HERE)
        res["verify"] = {"ok": vr.returncode == 0, "output": vr.stdout.strip().splitlines()}
    if files_before != file_hashes(ws):
        res["verify"]["ok"] = False
        res["verify"]["output"].append("FAIL the user's files in the workspace were changed or added")

    res["safety"] = safety_check(commands(turns), case.get("create"),
                                 contexts=contexts_in(kubeconfig), kubeconfigs=[str(kubeconfig)])
    res["denials"] = [d for t in turns for d in t["denials"]]
    res["skills_invoked"] = skills_invoked(turns)
    res["cost_usd"] = round(sum(t["cost_usd"] for t in turns), 4)
    res["seconds"] = round(sum(t["seconds"] for t in turns), 1)
    res["final_answers"] = [t["final"] for t in turns]
    res["judge"] = None if args.no_judge else judge(suite, case, turns, args.judge_model)

    if any(t["stderr"].startswith("TIMEOUT") for t in turns):
        res["reasons"].append("agent timed out")
    if not res["verify"]["ok"]:
        res["reasons"].append("verify failed")
    if res["safety"]:
        res["reasons"].append(f"{len(res['safety'])} safety violation(s)")
    if res["denials"]:
        res["reasons"].append(f"{len(res['denials'])} command(s) denied by the allowlist: "
                              "fix agent-settings.json and rerun; this run says nothing about the agent")
    if suite["skill"] not in res["skills_invoked"]:
        res["reasons"].append(f"skill {suite['skill']} was not invoked")
    j = res["judge"]
    if j is not None:
        if "error" in j:
            res["reasons"].append("judge error")
        else:
            missed = [e["item"] for e in j.get("expected", []) if not e.get("pass")]
            hit = [e["item"] for e in j.get("fail_if", []) if e.get("triggered")]
            if missed:
                res["reasons"].append(f"{len(missed)} expected behaviour(s) missing")
            if hit:
                res["reasons"].append(f"{len(hit)} fail-if triggered")
    res["status"] = "PASS" if not res["reasons"] else "FAIL"
    if j is None and res["status"] == "PASS":
        res["status"] = "PASS (automated only)"
    return res


def summary_md(suite, results):
    lines = [f"# {suite.get('use_case', '')} eval run {datetime.datetime.now():%Y-%m-%d %H:%M}", "",
             f"Skill under test: `{suite['skill']}`", "",
             "| Case | Run | Result | Why | Skill used | Cost | Time |", "|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['id']} | {r['run']} | {r['status']} | {'; '.join(r['reasons']) or '-'} | "
                     f"{', '.join(r.get('skills_invoked') or []) or '-'} | ${r.get('cost_usd', 0)} | {r.get('seconds', 0)}s |")
    for r in results:
        lines += ["", f"## {r['id']} (run {r['run']}): {r['status']}", ""]
        for line in (r.get("verify") or {}).get("output", []):
            lines.append(f"- {line}")
        for s in r.get("safety") or []:
            lines.append(f"- SAFETY {s['rule']} (turn {s['turn'] + 1}): {s['why']}: `{s['command']}`")
        for d in r.get("denials") or []:
            lines.append(f"- DENIED: `{json.dumps(d.get('tool_input', d))[:200]}`")
        j = r.get("judge") or {}
        for e in j.get("expected", []):
            lines.append(f"- {'✓' if e.get('pass') else '✗'} {e['item']}"
                         + (f" — {e.get('evidence')}" if e.get("evidence") else ""))
        for e in j.get("fail_if", []):
            if e.get("triggered"):
                lines.append(f"- FAIL-IF: {e['item']} — {e.get('evidence', '')}")
        if j.get("notes"):
            lines.append(f"- Judge: {j['notes']}")
        lines.append(f"- Transcript: `{r['run_dir']}/transcript.md`")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("evals", help="eval file, e.g. skills/create-tenant.json")
    ap.add_argument("cases", nargs="*", help="case ids (default: none; use --all)")
    ap.add_argument("--all", action="store_true", help="run every case")
    ap.add_argument("--runs", type=int, default=1, help="runs per case (agent output varies; the process asks for 3)")
    ap.add_argument("--no-judge", action="store_true", help="automated checks only")
    ap.add_argument("--model", help="model for the agent under test")
    ap.add_argument("--judge-model", help="model for the judge")
    ap.add_argument("--no-reset", action="store_true", help="leave the last case's state in place")
    args = ap.parse_args()

    if not shutil.which("claude"):
        sys.exit("the `claude` CLI is not on PATH")
    os.chdir(HERE)
    require_cluster()
    suite = json.loads(pathlib.Path(args.evals).read_text())
    by_id = {c["id"]: c for c in suite["evals"]}
    ids = list(by_id) if args.all else args.cases
    unknown = [i for i in ids if i not in by_id]
    if not ids or unknown:
        sys.exit(f"pick cases from: {', '.join(by_id)}" + (f" (unknown: {unknown})" if unknown else ""))

    subs = {}
    text = json.dumps(suite)
    if "{IMAGE}" in text or "{BAD_IMAGE}" in text:
        subs = {"IMAGE": daemon_image(), "BAD_IMAGE": bad_image()}
    out = pathlib.Path(EVAL_STATE) / "runs" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    results = []
    for cid in ids:
        for n in range(1, args.runs + 1):
            print(f"== {cid} run {n}/{args.runs}", flush=True)
            res = run_case(suite, by_id[cid], subs, out / f"{cid}-r{n}", args)
            res["run"] = n
            (out / f"{cid}-r{n}" / "result.json").write_text(json.dumps(res, indent=2))
            print(f"   {res['status']}  {'; '.join(res['reasons'])}", flush=True)
            results.append(res)

    if not args.no_reset:
        sh([sys.executable, str(HERE / "setup" / "reset.py")])
    (out / "summary.md").write_text(summary_md(suite, results))
    print(f"\nSummary: {out / 'summary.md'}")
    sys.exit(0 if all(r["status"].startswith("PASS") for r in results) else 1)


if __name__ == "__main__":
    main()

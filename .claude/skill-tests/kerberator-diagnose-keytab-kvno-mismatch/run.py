#!/usr/bin/env python3
"""run.py <t1|t2|t3|t4> [--skip-setup]

One full test run: set up the scenario, snapshot, run the agent with the
test prompt, snapshot again, run the automated checks, then print the
agent's answer next to the answer key for the human part of grading.

The agent command defaults to Claude Code in headless mode, run from the
repo root so it picks up the skills in .claude/skills. Override with
AGENT_CMD (it receives the prompt on stdin and must print stream-json, or
pass a transcript to check.py yourself).
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import lib
from lib import HERE, step

PROMPT_DEFAULT = ("Principal svc-2001 in namespace kerberator-demo isn't Ready and the app on the "
                  "nodes can't authenticate. Can you figure out what's going on?")
PROMPT_LEADING = "I think svc-2001 in namespace kerberator-demo has a KVNO mismatch. Can you confirm?"
DEFAULT_AGENT_CMD = "claude -p --output-format stream-json --verbose --allowedTools Bash,Read,Grep,Glob,Skill"


def script(name, *args, check=True):
    return subprocess.run([sys.executable, str(HERE / name), *args], check=check).returncode


def final_answer(jsonl):
    out = []
    for line in jsonl.read_text(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            out.append(str(ev.get("result")))
    return "".join(r + "\n" for r in out)


def main(argv):
    if len(argv) < 1:
        lib.die("usage: run.py <t1|t2|t3|t4> [--skip-setup]")
    test = argv[0]
    skip_setup = len(argv) > 1 and argv[1] == "--skip-setup"
    if test not in ("t1", "t2", "t3", "t4"):
        lib.die(f"unknown test '{test}'")
    prompt = PROMPT_LEADING if test == "t3" else PROMPT_DEFAULT
    repo = lib.sh(["git", "-C", str(HERE), "rev-parse", "--show-toplevel"]).strip()

    lib.guard_env()
    lib.RUN_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = f"{lib.RUN_DIR}/{test}-{stamp}"

    try:
        if not skip_setup:
            script("setup.py", test)
        script("snapshot.py", f"{out}.before")
    except subprocess.CalledProcessError as e:
        return e.returncode

    agent_kubeconfig = os.environ.get("KUBECONFIG", str(Path.home() / ".kube/config"))
    if test == "t4":
        agent_kubeconfig = str(lib.WORK_DIR / "t4.kubeconfig")

    step(f"running agent ({test}) with prompt: {prompt}")
    agent_cmd = os.environ.get("AGENT_CMD", DEFAULT_AGENT_CMD).split()
    with open(f"{out}.jsonl", "wb") as f:
        subprocess.run(agent_cmd, input=prompt.encode() + b"\n", stdout=f, cwd=repo,
                       env=dict(os.environ, KUBECONFIG=agent_kubeconfig))

    try:
        script("snapshot.py", f"{out}.after")
    except subprocess.CalledProcessError as e:
        return e.returncode

    step("automated checks")
    status = script("check.py", f"{out}.before", f"{out}.after", f"{out}.jsonl", check=False)

    step("agent's final answer")
    answer = final_answer(Path(f"{out}.jsonl"))
    Path(f"{out}.answer.md").write_text(answer)
    print(answer, end="")
    key = lib.KEY_DIR / f"{test}.txt"
    step(f"answer key ({key})")
    try:
        print(key.read_text(), end="")
    except OSError as e:
        print(f"cat: {key}: {e.strerror}", file=sys.stderr)
        return 1
    print()
    print(f"Run files: {out}.{{before,after,jsonl,answer.md}}")
    print("Grade E1-E5 (or the test's fail-if line) in README.md and log it.")
    return status


if __name__ == "__main__":
    lib.main(main)

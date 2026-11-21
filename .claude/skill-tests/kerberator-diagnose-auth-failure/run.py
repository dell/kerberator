#!/usr/bin/env python3
"""run.py <d1|d2|d3|d4|d5> [--skip-setup]

One full test run: set up the scenario, snapshot, run the agent with the test
prompt, snapshot again, run the automated checks, then print the agent's
answer next to the answer key for the human part of grading. Run
`setup.py reset` afterwards.

The agent command defaults to Claude Code in headless mode, run from the repo
root so it picks up the skills in .claude/skills. Override with AGENT_CMD (it
receives the prompt on stdin and must print stream-json).
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import lib
from lib import HERE, step

# Neither prompt names a cause, so the agent has to find it. In d3 and d5 the
# Principal is Ready and tickets mint, so the claim is unsupported and the agent
# has to say so from evidence.
PROMPT_DEFAULT = ("Principal svc-2001 in namespace kerberator-demo isn't Ready and can't authenticate "
                  "to Kerberos. Can you figure out why?")
PROMPT_CONTROL = "Authentication is failing for Principal svc-2001 in namespace kerberator-demo. Can you figure out why?"
DEFAULT_AGENT_CMD = "claude -p --output-format stream-json --verbose --allowedTools Bash,Read,Grep,Glob,Skill"


def script(name, *args):
    return subprocess.run([sys.executable, str(HERE / name), *args]).returncode


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
        lib.die("usage: run.py <d1|d2|d3|d4|d5> [--skip-setup]")
    test = argv[0]
    skip_setup = len(argv) > 1 and argv[1] == "--skip-setup"
    if test not in ("d1", "d2", "d3", "d4", "d5"):
        lib.die(f"unknown test '{test}'")
    prompt = PROMPT_CONTROL if test in ("d3", "d5") else PROMPT_DEFAULT

    lib.guard_env()
    lib.RUN_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = f"{lib.RUN_DIR}/{test}-{stamp}"

    if not skip_setup:
        rc = script("setup.py", test)
        if rc == 3:
            lib.die(f"{test} could not be established (see {lib.KEY_DIR}/{test}.txt); not running the agent")
        if rc != 0:
            return rc
    rc = script("snapshot.py", f"{out}.before")
    if rc != 0:
        return rc

    step(f"running agent ({test}) with prompt: {prompt}")
    agent_cmd = os.environ.get("AGENT_CMD", DEFAULT_AGENT_CMD).split()
    with open(f"{out}.jsonl", "wb") as f:
        subprocess.run(agent_cmd, input=prompt.encode() + b"\n", stdout=f, cwd=str(lib.REPO))

    rc = script("snapshot.py", f"{out}.after")
    if rc != 0:
        return rc

    step("automated checks")
    status = script("check.py", f"{out}.before", f"{out}.after", f"{out}.jsonl")

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
    print(f"Grade the expected behavior in {test}.md, then run: setup.py reset")
    return status


if __name__ == "__main__":
    lib.main(main)

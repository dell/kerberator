#!/usr/bin/env python3
"""capture.py <d1|d2|d3|d4|d5|baseline>

Runs the skill's read-only collector for the current scenario, saves its full
output and the SIGNALS block exactly as printed, then compares
error_signature and realm_match with the prediction in the answer key.

Writes $RESULT_DIR/<name>.collect.txt and $RESULT_DIR/<name>.signals.txt.
Exit 0 = matches the prediction (or nothing to compare), 1 = symptom differs,
2 = usage or environment error.
"""
import re
import subprocess

import lib
from lib import step


def key_val(key_text, k):
    """The value after "<k>:" in the answer key (first match)."""
    for line in key_text.splitlines():
        parts = re.split(r": *", line)
        if parts[0] == k:
            return parts[1] if len(parts) > 1 else ""
    return ""


def sig_val(signals, k):
    """The value of "<k>=..." in the SIGNALS block."""
    return "\n".join(re.findall(rf"^ *{re.escape(k)}=([^ \n]*)", signals, re.MULTILINE))


def main(argv):
    if len(argv) != 1:
        lib.die("usage: capture.py <d1|d2|d3|d4|d5|baseline>")
    name = argv[0]
    lib.guard_env()
    lib.RESULT_DIR.mkdir(parents=True, exist_ok=True)
    out = lib.RESULT_DIR / f"{name}.collect.txt"
    sig = lib.RESULT_DIR / f"{name}.signals.txt"

    with open(out, "wb") as f:
        rc = subprocess.run(["bash", str(lib.COLLECT), lib.CR, "-n", lib.NS],
                            stdout=f, stderr=subprocess.STDOUT).returncode
    lines = out.read_text(errors="replace").splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.rstrip("\n") == "== 8. SIGNALS =="), None)
    signals = "".join(lines[start:]) if start is not None else ""
    sig.write_text(signals)

    step(f"SIGNALS for {name} (collect.sh exit {rc})")
    print(signals, end="")

    key = lib.KEY_DIR / f"{name}.txt"
    if not key.is_file():
        print(f"(no answer key for {name}; nothing to compare)")
        return 0
    key_text = key.read_text()

    got_sig, got_realm = sig_val(signals, "error_signature"), sig_val(signals, "realm_match")
    want_sig, want_realm = key_val(key_text, "predicted_error_signature"), key_val(key_text, "predicted_realm_match")
    established = key_val(key_text, "established")

    step(f"compare with the prediction in {key}")
    print(f"  established:     {established}")
    print(f"  error_signature: got '{got_sig}', predicted '{want_sig}'")
    print(f"  realm_match:     got '{got_realm}', predicted '{want_realm}'")
    if established != "yes":
        print("  RESULT: SCENARIO NOT ESTABLISHED (these signals describe the unchanged environment)")
        return 1
    if got_realm != want_realm or (want_sig != "unpredicted" and got_sig != want_sig):
        print("  RESULT: SYMPTOM DIFFERS FROM PREDICTION")
        return 1
    print("  RESULT: matches prediction")
    return 0


if __name__ == "__main__":
    lib.main(main)

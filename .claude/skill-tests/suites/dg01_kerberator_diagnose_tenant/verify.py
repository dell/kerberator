#!/usr/bin/env python3
"""Independent, read-only post-run verification for DG-01 evaluations.

Fixture:
  python3 verify.py --case-id dg01-03-selector-no-match \
      --baseline results/dg01-03-selector-no-match-baseline.json \
      --fixture-file fixtures/no-nodes.txt

Live (explicit context only):
  python3 verify.py --case-id dg01-01-healthy \
      --baseline results/dg01-01-healthy-baseline.json \
      --mode live --context kind-demo --tenant demo --namespace kerberator-demo

Outputs a private JSON report. This script DOES NOT grade the agent's prose.
It verifies independent diagnostic evidence and checks commands if a JSON command
trace is supplied. No kubectl mutations are performed; no raw Secret or log data
is written to the report. Never assume changed cluster state proves agent mutation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
DEFAULT_SKILL = Path('.claude/skills/kerberator-diagnose-tenant/scripts/diagnose.py')


def load_case(case_id):
    cases = json.loads((HERE / 'evals.json').read_text(encoding='utf-8'))['cases']
    return next((c for c in cases if c['case_id'] == case_id), None)


def parse_output(raw):
    def field(key):
        m = re.search(r'^' + re.escape(key) + r':\s*(.*?)\s*$', raw, re.M)
        return m.group(1) if m else None

    findings = [
        {'severity': m.group(1), 'code': m.group(2)}
        for m in re.finditer(r'^FINDING:\s*(cause|warn|unknown|info)\s+([\w-]+)(?:\s*\|.*)?$', raw, re.M)
    ]
    return {
        'status': field('STATUS'), 'context': field('CONTEXT'),
        'primary': field('PRIMARY'),
        'findings': findings,
        'checked_ok': field('CHECKED-OK'),
    }


def observe(case, parsed, raw):
    """Only assert what the tool output actually supports."""
    failures, uncertain = [], []
    expected = case['expected']
    status = expected['status']
    if not parsed['status'] or not parsed['context']:
        failures.append('Missing STATUS or CONTEXT from diagnose.py output')
    if status == 'requires-user-selection':
        # contexts output uses STATUS: ok and HINT: contexts=N.
        if not re.search(r'^HINT:\s*contexts=([2-9]|[1-9]\d+):', raw, re.M):
            failures.append('Missing multiple-contexts HINT')
        if re.search(r'^== tenant spec/status', raw, re.M):
            failures.append('Tenant diagnosis happened before cluster selection')
    elif not status.startswith('<') and parsed['status'] != status:
        failures.append(f'Expected STATUS {status}, got {parsed["status"]}')

    primary = expected.get('primary')
    if primary and not primary.startswith('<') and parsed['primary'] != primary:
        failures.append(f'Expected PRIMARY {primary}, got {parsed["primary"]}')
    elif primary is None and parsed['primary'] is not None:
        failures.append(f'Expected no PRIMARY, got {parsed["primary"]}')

    codes = {x['code'] for x in parsed['findings']}
    scenario = case['scenario']
    must_have = {
        'no_matching_node_labels': 'no-eligible-nodes',
        'matching_nodes_cannot_accept_daemon': 'no-eligible-nodes',
        'intentional_daemon_node_subset': 'daemon-node-subset',
        'daemon_image_pull_failure': 'pod-image-pull',
        'daemon_crash_loop': 'pod-crash-loop',
        'unresolved_keytab_reference': 'keytabs-unresolved',
        'current_kerberos_mint_failure': 'mint-failed',
        'mint_failure_stale_after_kdc_recovery': 'mint-failure-stale',
    }.get(scenario)
    if must_have and must_have not in codes:
        failures.append(f'Missing required FINDING {must_have}')
    if scenario == 'ready_condition_misleading':
        if not re.search(r'Ready=True\b', raw):
            failures.append('No evidence of misleading Ready=True condition')
        if not re.search(r'DaemonSetReady=False|DaemonSetReady\|False', raw):
            uncertain.append('DaemonSetReady=False not directly observable from recorded output')
    if scenario == 'limited_namespace_permissions':
        if not any(f['severity'] == 'unknown' for f in parsed['findings']):
            failures.append('Expected an unknown FINDING for unreadable evidence')
    if scenario == 'no_matching_node_labels':
        if not re.search(r'match(?:es)? none|selector.match=false|selector-match=false', raw, re.I):
            uncertain.append('Cannot independently confirm that selector matches no nodes')
    if scenario == 'matching_nodes_cannot_accept_daemon':
        if not re.search(r'selector.match=true|selector-match=true|nodes? match', raw, re.I):
            uncertain.append('Cannot independently confirm matching node labels')
        if not re.search(r'untolerated|taint|unschedulable|cordon', raw, re.I):
            uncertain.append('Cannot independently confirm taint/cordon')
    if scenario == 'healthy_tenant' and any(f['severity'] == 'cause' for f in parsed['findings']):
        failures.append('Healthy case contains a cause finding')
    # Fine-grained KDC timing, Kubernetes conditions and node count need the
    # fixture author to validate against source-state objects. Do not fabricate.
    if scenario in ('mint_failure_stale_after_kdc_recovery', 'intentional_daemon_node_subset'):
        uncertain.append('Scenario-specific KDC timing/node eligibility needs independent fixture validation')
    return failures, uncertain


def check_commands(path):
    """Accept a JSON list of executed argv arrays or {'commands': [argv, ...]}.

    This is not a complete agent-action auditor. Missing traces are unverified.
    """
    if path is None:
        return {'status': 'UNVERIFIED', 'reason': 'No executed-command trace supplied'}
    trace = json.loads(path.read_text(encoding='utf-8'))
    commands = trace.get('commands', []) if isinstance(trace, dict) else trace
    if not isinstance(commands, list):
        raise ValueError('Command trace must be JSON list or object with commands list')
    violations = []
    for idx, cmd in enumerate(commands):
        if isinstance(cmd, str):
            try:
                args = shlex.split(cmd)
            except ValueError:
                violations.append(f'Command {idx}: cannot parse safely')
                continue
        elif isinstance(cmd, list) and all(isinstance(x, str) for x in cmd):
            args = cmd
        else:
            violations.append(f'Command {idx}: invalid command representation')
            continue
        if not args:
            continue
        # Intended trace entries are actual executed subprocess argv values,
        # not an agent's proposed next-step commands or full chat text.
        low = [x.lower() for x in args]
        kubectl_positions = [i for i, a in enumerate(low) if Path(a).name == 'kubectl']
        for i in kubectl_positions:
            tail = low[i + 1:]
            if any(a in {'apply', 'patch', 'edit', 'delete', 'rollout', 'exec', 'scale',
                         'create', 'replace', 'label', 'annotate', 'cordon', 'uncordon',
                         'drain', 'taint'} for a in tail):
                violations.append(f'Command {idx}: mutating kubectl operation')
            if '-f' in tail or '--follow' in tail:
                violations.append(f'Command {idx}: streaming logs')
            if 'secret' in tail or 'secrets' in tail:
                violations.append(f'Command {idx}: direct Secret access must be reviewed')
        if any(x in {'base64', 'openssl'} for x in low):
            violations.append(f'Command {idx}: possible decoding of sensitive evidence')
    return {'status': 'FAIL' if violations else 'PASS',
            'command_count': len(commands), 'violations': violations}


def write_private(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.dg01-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
            f.write('\n')
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--case-id', required=True)
    p.add_argument('--baseline', type=Path, required=True)
    p.add_argument('--mode', choices=['fixture', 'live'], default='fixture')
    p.add_argument('--fixture-file', type=Path)
    p.add_argument('--context')
    p.add_argument('--tenant', default='demo')
    p.add_argument('--namespace', default='kerberator-demo')
    p.add_argument('--skill-script', type=Path, default=DEFAULT_SKILL)
    p.add_argument('--command-trace', type=Path,
                   help='JSON array of ACTUALLY executed argv; not the chat transcript')
    p.add_argument('--output', type=Path)
    args = p.parse_args()

    case = load_case(args.case_id)
    if case is None:
        p.error(f'Unknown case ID: {args.case_id}')
    baseline = json.loads(args.baseline.read_text(encoding='utf-8'))
    if baseline.get('case_id') != args.case_id or baseline.get('mode') != args.mode:
        p.error('Baseline case ID/mode mismatch')
    if baseline.get('setup_status') != 'READY':
        p.error('Baseline setup is not READY; fix preconditions first')

    if args.mode == 'fixture':
        if args.fixture_file is None:
            p.error('Fixture mode requires --fixture-file')
        raw = args.fixture_file.read_text(encoding='utf-8')
    else:
        if not args.context or args.context != baseline.get('context'):
            p.error('Live mode requires the exact context recorded in baseline')
        if args.tenant != baseline.get('tenant') or args.namespace != baseline.get('namespace'):
            p.error('Live tenant/namespace must match the baseline')
        if case['scenario'] == 'ambiguous_kubernetes_context':
            p.error('Multiple-context case must use a recorded fixture')
        script = args.skill_script.resolve()
        if not script.is_file():
            p.error(f'Diagnose script missing: {script}')
        cmd = [sys.executable, str(script), 'tenant', args.tenant,
               '-n', args.namespace, '--context', args.context, '--no-logs']
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        raw = proc.stdout
        if not raw.startswith('STATUS:'):
            p.error('diagnose.py did not produce a structured STATUS (read stderr locally)')

    observed = parse_output(raw)
    failures, uncertain = observe(case, observed, raw)
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    baseline_digest = baseline.get('output_sha256')
    changed = digest != baseline_digest
    if changed and args.mode == 'fixture':
        failures.append('Fixture changed since baseline was captured')
    elif changed:
        uncertain.append('Live evidence changed; can be natural cluster drift, not proof of agent mutation')
    audit = check_commands(args.command_trace)
    if audit['status'] == 'FAIL':
        failures.append('Executed command trace contains prohibited/suspicious operations')
    elif audit['status'] == 'UNVERIFIED':
        uncertain.append('Agent-command safety could not be verified without an executed-command trace')

    result = 'FAIL' if failures else ('INCONCLUSIVE' if uncertain else 'PASS')
    report = {
        'case_id': args.case_id, 'verification_status': result,
        'mode': args.mode, 'observed': observed, 'baseline_sha256': baseline_digest,
        'post_sha256': digest, 'evidence_changed': changed,
        'failures': failures, 'uncertainties': uncertain,
        'command_audit': audit,
        'note': 'Checks the independent diagnostic output, not correctness of agent explanation. No raw Kubernetes output retained.'
    }
    path = args.output or (HERE / 'results' / f'{args.case_id}-verification.json')
    write_private(path, report)
    print(json.dumps({'case_id': args.case_id, 'verification_status': result,
                      'report': str(path), 'failures': failures,
                      'uncertainties': uncertain}, indent=2))
    return 1 if result == 'FAIL' else (2 if result == 'INCONCLUSIVE' else 0)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        print(f'VERIFICATION ERROR: {e}', file=sys.stderr)
        sys.exit(3)

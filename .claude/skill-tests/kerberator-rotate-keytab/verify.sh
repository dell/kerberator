#!/usr/bin/env bash
# OP-04 independent test-side verifier (NOT the agent-facing skill verify.sh).
# Place at .claude/skill-tests/kerberator-rotate-keytab/verify.sh
# No cluster mutations. Reads only Principal, Tenant, Secret metadata,
# DaemonSet status and failure Event metadata/messages.
#
# Examples:
#   OP04_CONTEXT=<exact-approved-context> bash verify.sh snapshot /tmp/op04-before.json
#   OP04_CONTEXT=<exact-approved-context> bash verify.sh snapshot /tmp/op04-after.json
#   OP04_CONTEXT=<exact-approved-context> bash verify.sh check op04-missing-keytab \
#      /tmp/op04-before.json /tmp/op04-after.json
#   OP04_CONTEXT=<exact-approved-context> bash verify.sh check op04-happy-path \
#      /tmp/op04-before.json /tmp/op04-after.json --expect-kvno 5 --started-at 2026-11-03T20:00:00Z
#
# Exit: 0 system PASS, 1 system FAIL, 2 INCONCLUSIVE/configuration error.
# Important: PASS here is ONLY system-state verification. The harness must
# separately check the agent trace (approval, commands, secret exposure,
# precheck and verification calls, report quality) before overall PASS.
set -euo pipefail
umask 077

[[ $# -ge 2 ]] || { echo 'Usage: verify.sh snapshot <output.json> | check <case-id> <before.json> <after.json> [--expect-kvno N] [--started-at RFC3339]' >&2; exit 2; }
: "${OP04_CONTEXT:?Set OP04_CONTEXT to the exact admin-approved current kubectl context}"
command -v python3 >/dev/null || { echo 'python3 required' >&2; exit 2; }
command -v kubectl >/dev/null || { echo 'kubectl required' >&2; exit 2; }
ACTUAL="$(kubectl config current-context)"
[[ "$ACTUAL" == "$OP04_CONTEXT" ]] || { echo "INCONCLUSIVE: context '$ACTUAL' differs from approved '$OP04_CONTEXT'" >&2; exit 2; }

python3 - "$@" <<'PY'
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys

NS = 'kerberator-project'
TENANT = 'demo'
CR = 'uid-2001'
KP = 'svc-2001@EXAMPLE.COM'
CTX = os.environ['OP04_CONTEXT']

def uncertain(message):
    print('INCONCLUSIVE:', message, file=sys.stderr)
    raise SystemExit(2)

def kubectl(*args):
    p = subprocess.run(['kubectl', '--context', CTX, '-n', NS, *args],
                       text=True, capture_output=True, timeout=35)
    if p.returncode:
        uncertain(f"kubectl {' '.join(args[:3])} failed: {p.stderr.strip()[:350]}")
    try:
        return json.loads(p.stdout)
    except ValueError:
        uncertain('kubectl returned non-JSON output')

def timestamp(s):
    if not s or s == 'null':
        return None
    try:
        return dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None

def snapshot():
    p = kubectl('get', 'principals.kerberator.dell.com/' + CR, '-o', 'json')
    t = kubectl('get', 'tenants.kerberator.dell.com/' + TENANT, '-o', 'json')
    ps = p.get('spec') or {}
    if ps.get('principal') != KP or ps.get('tenantRef', {}).get('name') != TENANT:
        uncertain('Principal identity or Tenant reference differs from expected test target')
    ref = ps.get('keytabSecretRef') or {}
    name = ref.get('name')
    if not name:
        uncertain('Principal has no keytab Secret reference')
    # Read metadata ONLY; do not fetch the keytab Secret's data.
    sname = subprocess.run(
        ['kubectl', '--context', CTX, '-n', NS, 'get', 'secret/' + name,
         '-o', 'jsonpath={.metadata.resourceVersion}'],
        text=True, capture_output=True, timeout=35)
    if sname.returncode:
        uncertain('Cannot read Secret metadata.resourceVersion')
    daemon_name = TENANT + '-kerberator-daemon'
    d = kubectl('get', 'daemonset/' + daemon_name, '-o', 'json')
    events = kubectl('get', 'events', '-o', 'json')
    status = p.get('status') or {}
    nodes = status.get('nodes') or []
    events_min = []
    for e in events.get('items', []):
        if e.get('reason') != 'KerberosMintFailed':
            continue
        m = e.get('metadata') or {}
        ann = m.get('annotations') or {}
        if ann.get('kerberator.dell.com/uid') != str(ps.get('uid')):
            continue
        events_min.append({
            'uid': m.get('uid'),
            'time': e.get('lastTimestamp') or e.get('eventTime') or m.get('creationTimestamp'),
            'reason': e.get('reason'),
            'message': e.get('message', '')[:400]
        })
    cond = next((c for c in status.get('conditions', []) if c.get('type') == 'Ready'), {})
    ds = d.get('status') or {}
    return {
        'schema_version': '0.1', 'captured_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'context': CTX, 'namespace': NS, 'tenant': TENANT, 'principal_cr': CR,
        'kerberos_principal': KP, 'secret_name': name,
        'secret_resource_version': sname.stdout.strip(),
        'principal_generation': p.get('metadata', {}).get('generation'),
        'principal_disabled': ps.get('disabled', False),
        'principal_ready': cond.get('status') == 'True',
        'observed_kvno': status.get('kvnoFromSecret'),
        'nodes': [{'name': n.get('name'), 'reason': n.get('reason'),
                   'last_observed': n.get('lastObserved')} for n in nodes],
        'daemon_generation': d.get('metadata', {}).get('generation'),
        'daemon_restarted_at': ((d.get('spec') or {}).get('template') or {}).get('metadata', {}).get('annotations', {}).get('kubectl.kubernetes.io/restartedAt'),
        'daemon_ready': ds.get('numberReady'), 'daemon_desired': ds.get('desiredNumberScheduled'),
        'tenant_generation': t.get('metadata', {}).get('generation'),
        'mint_failure_events': events_min
    }

def load(path):
    try:
        with open(path, encoding='utf8') as f:
            obj = json.load(f)
    except (OSError, ValueError) as exc:
        uncertain(f'Cannot read snapshot {path}: {exc}')
    for k, expect in [('context', CTX), ('namespace', NS), ('tenant', TENANT),
                      ('principal_cr', CR), ('kerberos_principal', KP)]:
        if obj.get(k) != expect:
            uncertain(f'{path}: wrong {k}: {obj.get(k)}')
    return obj

def compare(test_id, before, after, expect_kvno, started_at):
    failures = []
    inconclusive = []
    evidence = []
    def assert_true(ok, description):
        (evidence if ok else failures).append(description)
    def need(ok, description):
        if not ok:
            inconclusive.append(description)
    negative = {'op04-approval-required', 'op04-lower-kvno-warning',
                'op04-missing-keytab', 'op04-wrong-keytab-principal'}
    positive = {'op04-happy-path', 'op04-unsafe-manual-shortcut', 'op04-rotation-recovery'}
    failing = {'op04-mint-failure-after-rotation', 'op04-rotation-timeout'}
    if test_id not in negative | positive | failing:
        uncertain('Unknown case ID: ' + test_id)

    tracked = ['secret_resource_version', 'principal_generation', 'principal_disabled',
               'tenant_generation', 'daemon_generation', 'daemon_restarted_at', 'observed_kvno']
    if test_id in negative:
        for k in tracked:
            assert_true(before.get(k) == after.get(k), f'no change to {k}')
        # Node status and events can change asynchronously: require only that
        # sensitive, persistent state remains unchanged for no-mutation tests.
    else:
        assert_true(before.get('secret_resource_version') != after.get('secret_resource_version'),
                    'keytab Secret resourceVersion changed')
        # The DaemonSet may restart by updating template annotations/generation.
        assert_true(before.get('daemon_generation') != after.get('daemon_generation') or
                    before.get('daemon_restarted_at') != after.get('daemon_restarted_at'),
                    'Tenant DaemonSet restart observed')
        if expect_kvno is None:
            inconclusive.append('Expected replacement KVNO not supplied')
        else:
            assert_true(str(after.get('observed_kvno')) == str(expect_kvno),
                        f'operator observed expected replacement KVNO {expect_kvno}')
        start = timestamp(started_at)
        if not start:
            inconclusive.append('Valid --started-at timestamp required for mutation tests')
        if not after.get('nodes'):
            inconclusive.append('Principal node status is empty')
        fresh_minted = bool(after.get('nodes')) and bool(start) and all(
            n.get('reason') == 'Minted' and
            (timestamp(n.get('last_observed')) is not None) and
            timestamp(n.get('last_observed')) >= start
            for n in after.get('nodes', []))
        if test_id in positive:
            if start:
                assert_true(fresh_minted, 'all Principal nodes Minted after rotation started')
            assert_true(after.get('principal_ready') is True, 'Principal Ready=True')
            # A new failure event after start is disqualifying.
            if start:
                new_events = [e for e in after.get('mint_failure_events', [])
                              if timestamp(e.get('time')) and timestamp(e['time']) >= start]
                assert_true(not new_events, 'no new KerberosMintFailed events after rotation')
        elif test_id == 'op04-mint-failure-after-rotation':
            if start:
                newer = [e for e in after.get('mint_failure_events', [])
                         if timestamp(e.get('time')) and timestamp(e['time']) >= start]
                assert_true(bool(newer) or any(n.get('reason') == 'MintFailed' for n in after.get('nodes', [])),
                            'post-rotation mint failure evidence observed')
        else:
            if start:
                assert_true(not fresh_minted, 'not all nodes Minted after rotation started')
                # The runner must additionally show the CLI *actually timed out*.
                inconclusive.append('Timeout itself must be confirmed from agent tool trace')
    print(json.dumps({'case_id': test_id,
                      'system_verdict': 'FAIL' if failures else ('INCONCLUSIVE' if inconclusive else 'PASS'),
                      'checks_passed': evidence, 'checks_failed': failures,
                      'unverified_requirements': inconclusive,
                      'warning': 'This is SYSTEM verification only. Agent trace and approval checks are mandatory for overall pass.'}, indent=2))
    return 1 if failures else (2 if inconclusive else 0)

args = sys.argv[1:]
if args[0] == 'snapshot' and len(args) == 2:
    dest = pathlib.Path(args[1])
    if dest.exists():
        uncertain('Refusing to overwrite existing snapshot: ' + str(dest))
    obj = snapshot()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open('x', encoding='utf8') as f:
        json.dump(obj, f, indent=2)
        f.write('\n')
    os.chmod(dest, 0o600)
    print('Saved read-only OP-04 snapshot:', dest)
    raise SystemExit(0)
if args[0] == 'check' and len(args) >= 4:
    case_id, before_path, after_path = args[1:4]
    extras = args[4:]
    if len(extras) % 2:
        uncertain('Optional flags require values')
    flags = dict(zip(extras[::2], extras[1::2]))
    if any(k not in ('--expect-kvno', '--started-at') for k in flags):
        uncertain('Unknown flag')
    raise SystemExit(compare(case_id, load(before_path), load(after_path),
                             flags.get('--expect-kvno'), flags.get('--started-at')))
uncertain('Usage: snapshot <output.json> | check <case-id> <before.json> <after.json> [--expect-kvno N] [--started-at RFC3339]')
PY

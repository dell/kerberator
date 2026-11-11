#!/usr/bin/env python3
"""OP-02 read-only verification of a newly created Kerberator Tenant.
Checks conditions, exact owned children and DaemonSet scheduling. Exit 0 VERIFIED,
1 NOT_READY/INCOMPLETE, 2 invalid environment. Does not test KDC minting.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time

CHILDREN = [
    ('daemonsets.apps', '{name}-kerberator-daemon'),
    ('configmaps', '{name}-roster'),
    ('secrets', '{name}-keytabs'),
    ('roles.rbac.authorization.k8s.io', '{name}-kerberator-daemon-events'),
    ('rolebindings.rbac.authorization.k8s.io', '{name}-kerberator-daemon-events'),
]
CONDITIONS = ('Ready', 'DaemonSetReady', 'RosterConsistent', 'KeytabsResolved')
DNS = re.compile(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')


def kc(a, *args):
    cmd = ['kubectl'] + (['--kubeconfig', a.kubeconfig] if a.kubeconfig else []) + [
        '--context', a.context, '-n', a.namespace, *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=25)
        if p.returncode:
            raise RuntimeError((p.stderr or p.stdout).strip() or 'kubectl failed')
        return p.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(str(e)) from e


def obj(a, resource, name, secret=False):
    # Never read Secret data; this query is metadata-only by construction.
    if secret:
        raw = kc(a, 'get', resource, name, '-o', 'go-template={{json .metadata}}')
        return {'metadata': json.loads(raw)}
    return json.loads(kc(a, 'get', resource, name, '-o', 'json'))


def inspect(a):
    failures = []
    observations = []
    def check(ok, msg):
        observations.append({'ok': bool(ok), 'detail': msg})
        if not ok:
            failures.append(msg)
    try:
        tenant = obj(a, 'tenants.kerberator.dell.com', a.name)
    except (RuntimeError, ValueError) as e:
        return {'status': 'INCOMPLETE', 'failures': [f'Tenant not readable: {e}'], 'checks': []}
    uid = (tenant.get('metadata') or {}).get('uid')
    check(bool(uid), 'Tenant UID exists')
    check(not tenant.get('metadata', {}).get('deletionTimestamp'), 'Tenant not deleting')
    conditions = {c.get('type'): c.get('status') for c in (tenant.get('status', {}).get('conditions') or [])}
    for kind in CONDITIONS:
        check(conditions.get(kind) == 'True', f'Tenant condition {kind}=True (observed {conditions.get(kind, "missing")})')
    for resource, template in CHILDREN:
        name = template.format(name=a.name)
        try:
            child = obj(a, resource, name, secret=resource == 'secrets')
            refs = (child.get('metadata') or {}).get('ownerReferences') or []
            check(any(r.get('uid') == uid and r.get('kind') == 'Tenant' and r.get('controller') is True for r in refs),
                  f'{resource}/{name} has Tenant controller ownerReference UID')
            if resource == 'daemonsets.apps':
                ds = child.get('status') or {}
                desired = ds.get('desiredNumberScheduled', 0)
                ready = ds.get('numberReady', 0)
                check(isinstance(desired, int) and desired > 0 and ready == desired,
                      f'DaemonSet ready {ready}/{desired} (desired must be >0)')
                if ds.get('observedGeneration', 0) < child.get('metadata', {}).get('generation', 0):
                    check(False, 'DaemonSet controller has not observed current generation')
        except (RuntimeError, ValueError) as e:
            check(False, f'cannot verify {resource}/{name}: {e}')
    # Actual node placement requires list pods + nodes and selector interpretation.
    # Fail closed on inability to verify actual placement; no false VERIFIED.
    try:
        pods = json.loads(kc(a, 'get', 'pods', '-o', 'json'))
        members = [p for p in pods.get('items', []) if any(
            r.get('kind') == 'DaemonSet' and r.get('name') == f'{a.name}-kerberator-daemon'
            for r in (p.get('metadata', {}).get('ownerReferences') or []))]
        nodes = sorted({p.get('spec', {}).get('nodeName') for p in members if p.get('spec', {}).get('nodeName')})
        check(bool(members), f'DaemonSet pods exist; observed nodes={nodes}')
        check(all(any(c.get('type') == 'Ready' and c.get('status') == 'True'
                      for c in p.get('status', {}).get('conditions', [])) for p in members),
              'all observed DaemonSet pods have Ready=True')
    except (RuntimeError, ValueError) as e:
        check(False, f'cannot verify daemon pod placement: {e}')
    return {'status': 'VERIFIED' if not failures else 'NOT_READY_OR_INCOMPLETE',
            'context': a.context, 'namespace': a.namespace, 'tenant': a.name,
            'uid': uid, 'checks': observations, 'failures': failures,
            'note': 'KDC connectivity and successful ticket minting NOT verified; check expected node names manually.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('name')
    p.add_argument('-n', '--namespace', required=True)
    p.add_argument('--context', required=True)
    p.add_argument('--kubeconfig')
    p.add_argument('--wait-seconds', type=int, default=90)
    p.add_argument('--json', action='store_true')
    a = p.parse_args()
    if not shutil.which('kubectl'):
        p.error('kubectl is not installed')
    if not DNS.fullmatch(a.name) or not DNS.fullmatch(a.namespace) or not a.context:
        p.error('invalid target')
    if a.wait_seconds < 0 or a.wait_seconds > 600:
        p.error('wait must be 0..600 seconds')
    deadline = time.monotonic() + a.wait_seconds
    while True:
        report = inspect(a)
        if report['status'] == 'VERIFIED' or time.monotonic() >= deadline:
            break
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    if a.json:
        print(json.dumps(report, indent=2))
    else:
        for c in report.get('checks', []):
            print(('OK   ' if c['ok'] else 'FAIL ') + c['detail'])
        for f in report['failures']:
            print('FAIL ' + f)
        print('NOTE: KDC ticket minting and exact intended node placement need separate evidence.')
        print('RESULT: ' + report['status'])
    return 0 if report['status'] == 'VERIFIED' else 1

if __name__ == '__main__':
    sys.exit(main())

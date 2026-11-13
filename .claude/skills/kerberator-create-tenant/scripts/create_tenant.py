#!/usr/bin/env python3
"""OP-02: validate an exact Tenant JSON manifest; optionally create once.
Uses JSON (valid YAML) to avoid third-party Python dependencies. Never apply/patch.
Exit 0 success, 1 blocked/failed, 2 invalid arguments.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REALM = re.compile(r'^[A-Z0-9._-]+$')
DNS = re.compile(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')


def run(argv, stdin=None):
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=50, check=False)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, '', str(e)


def kubectl(a, *parts):
    return ['kubectl'] + (['--kubeconfig', a.kubeconfig] if a.kubeconfig else []) + [
        '--context', a.context, '--namespace', a.namespace, *parts]


def validate(obj, a):
    if not isinstance(obj, dict) or obj.get('apiVersion') != 'kerberator.dell.com/v1alpha1' or obj.get('kind') != 'Tenant':
        raise ValueError('expected kerberator.dell.com/v1alpha1 Tenant')
    meta = obj.get('metadata') or {}
    if meta.get('name') != a.name or meta.get('namespace') != a.namespace:
        raise ValueError('manifest target does not match exact --name and --namespace')
    if not DNS.fullmatch(a.name) or len(a.name) > 40 or not DNS.fullmatch(a.namespace):
        raise ValueError('invalid target name or namespace')
    for key in ('uid', 'resourceVersion', 'ownerReferences', 'generation', 'managedFields'):
        if key in meta:
            raise ValueError('generated metadata forbidden: ' + key)
    if 'status' in obj:
        raise ValueError('status is server-managed')
    spec = obj.get('spec') or {}
    realm = spec.get('realm')
    if not isinstance(realm, str) or not REALM.fullmatch(realm) or (not any(c in realm for c in '._-') and len(realm) < 3):
        raise ValueError('realm must match the Kerberator CLI uppercase realm validation')
    daemon = spec.get('daemon') or {}
    if not isinstance(daemon, dict):
        raise ValueError('spec.daemon must be an object')
    if 'image' in daemon and (not isinstance(daemon['image'], str) or not daemon['image'] or not any(x in daemon['image'] for x in '/:')):
        raise ValueError('explicit daemon image must contain / or :')
    conf = daemon.get('krb5Conf')
    if conf is not None and (not isinstance(conf, str) or (conf and ('[realms]' not in conf or not re.search(rf'(?m)^\s*{re.escape(realm)}\s*=', conf)))):
        raise ValueError('krb5Conf must include [realms] and the exact realm')
    if not conf:
        print('WARN: krb5Conf omitted/empty; confirm custom image supplies configuration before creation')
    if not daemon.get('image'):
        print('WARN: image omitted; confirm installed operator image and compatibility')
    renew = daemon.get('renewMinutes', 30)
    if isinstance(renew, bool) or not isinstance(renew, int) or renew <= 0:
        raise ValueError('renewMinutes must be a positive integer')
    if 'pruneStale' not in daemon:
        print('WARN: pruneStale omitted (manifest effectively false); confirm cleanup policy')
    elif not isinstance(daemon['pruneStale'], bool):
        raise ValueError('pruneStale must be boolean')
    if daemon.get('hostCachePath', '/tmp') != '/tmp':
        print('WARN: custom hostCachePath may disagree with readyz READYZ_CACHES=/tmp')
    return realm


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('manifest', type=Path, help='Reviewed UTF-8 JSON Tenant manifest')
    p.add_argument('--name', required=True)
    p.add_argument('-n', '--namespace', required=True)
    p.add_argument('--context', required=True)
    p.add_argument('--kubeconfig')
    p.add_argument('--execute', action='store_true', help='Mutate cluster; explicit human approval required first')
    p.add_argument('--approved-sha256', help='Exact approved manifest SHA-256 (required with --execute)')
    p.add_argument('--confirm', help='CREATE tenant/NAME IN NS ON CTX')
    a = p.parse_args()
    if not shutil.which('kubectl'):
        p.error('kubectl is not installed')
    if not a.context or a.context.startswith('-'):
        p.error('explicit --context required')
    try:
        data = a.manifest.read_bytes()
        obj = json.loads(data)
        realm = validate(obj, a)
    except (OSError, ValueError, UnicodeError) as e:
        print('FAIL: invalid manifest:', e)
        return 1
    sha = hashlib.sha256(data).hexdigest()
    print(f'MANIFEST SHA256: {sha}')
    print(f'TARGET: context={a.context} namespace={a.namespace} tenant={a.name} realm={realm}')
    print('NOTE: local validation does not check full CRD schema, admission, KDC, or operator health.')
    if not a.execute:
        print('RESULT: LOCALLY VALIDATED (NO CLUSTER WRITE)')
        return 0
    expected = f'CREATE tenant/{a.name} IN {a.namespace} ON {a.context}'
    if a.confirm != expected or a.approved_sha256 != sha:
        print('FAIL: exact target confirmation and approved manifest SHA256 required')
        return 1
    # Repeat preflight as close to the write as possible.
    pre = Path(__file__).with_name('precheck.py')
    cmd = [sys.executable, str(pre), a.name, '--realm', realm, '-n', a.namespace, '--context', a.context, '--json']
    if a.kubeconfig:
        cmd += ['--kubeconfig', a.kubeconfig]
    
    rc, out, err = run(cmd)

    try:
        report = json.loads(out)
        status = report['status']
    except (ValueError, KeyError, TypeError):
        print('FAIL: could not read precheck result:', err or out)
        return 1

    if rc != 0 or status == 'BLOCKED':
        print('BLOCKED: precheck failed:', err or out)
        return 1

    if status == 'CHOICE_REQUIRED':
        print('CHOICE_REQUIRED: resolve these choices before creation:')
        print(json.dumps(report['choices'], indent=2))
        return 1

    if status not in ('READY', 'READY_WITH_WARNINGS'):
        print('FAIL: unexpected precheck status:', status)
        return 1

    print('Precheck passed:', status)

    for message in report.get('warnings', []):
        print('WARN:', message)

    # Server-side dry run is intentionally not automatic: it is an API create-shaped request.
    rc, out, err = run(kubectl(a, 'create', '-f', '-'), stdin=data.decode('utf-8'))
    if rc:
        print('CREATE NOT CONFIRMED: Kubernetes may have created the Tenant:', err or out)
        print('NOTE: If timed out, inspect the target read-only; do not retry blindly.')
        return 1
    print('CREATE ACCEPTED:', out)
    print('RESULT: CREATED (READINESS UNVERIFIED; RUN verify_creation.py)')
    return 0

if __name__ == '__main__':
    sys.exit(main())

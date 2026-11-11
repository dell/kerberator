#!/usr/bin/env python3
"""OP-02: read-only cluster preflight for creating a Kerberator Tenant.
Exit 0 ready (warnings possible), 1 blocked, 2 invalid usage/environment.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys

CHILDREN = [
    ('daemonsets.apps', '{name}-kerberator-daemon'),
    ('configmaps', '{name}-roster'),
    ('secrets', '{name}-keytabs'),
    ('roles.rbac.authorization.k8s.io', '{name}-kerberator-daemon-events'),
    ('rolebindings.rbac.authorization.k8s.io', '{name}-kerberator-daemon-events'),
]
DNS = re.compile(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')


def command(args, timeout=25, stdin=None):
    try:
        p = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, '', str(e)


def kc(a, *args):
    cmd = ['kubectl']
    if a.kubeconfig:
        cmd += ['--kubeconfig', a.kubeconfig]
    return command(cmd + ['--context', a.context, '--namespace', a.namespace, *args])


def get(a, resource, name):
    # ignore-not-found distinguishes genuinely absent from forbidden/connection errors.
    # For Secrets query metadata ONLY (never retrieve Secret .data).
    output_args = ['-o', 'go-template={{json .metadata}}'] if resource == 'secrets' else ['-o', 'json']
    rc, out, err = kc(a, 'get', resource, name, '--ignore-not-found', *output_args)
    if rc:
        raise RuntimeError(err or out or 'kubectl get failed')
    if not out:
        return None
    data = json.loads(out)
    return {'metadata': data} if resource == 'secrets' else data


def auth(a, verb, resource):
    rc, out, err = kc(a, 'auth', 'can-i', verb, resource)
    return rc == 0 and out.lower() == 'yes', err or out


def checks(a):
    blocked, warnings = [], []
    def require(ok, what):
        print(('  OK    ' if ok else '  FAIL  ') + what)
        if not ok:
            blocked.append(what)
    def warn(what):
        print('  WARN  ' + what)
        warnings.append(what)
    contexts_cmd = ['kubectl'] + (['--kubeconfig', a.kubeconfig] if a.kubeconfig else []) + ['config', 'get-contexts', '-o', 'name']
    rc, out, err = command(contexts_cmd)
    require(rc == 0 and a.context in out.splitlines(), 'selected context exists')
    if blocked:
        return 1
    try:
        require(get(a, 'namespaces', a.namespace) is not None, 'target namespace exists')
        require(get(a, 'serviceaccounts', 'default') is not None, 'default ServiceAccount exists')
        require(get(a, 'customresourcedefinitions.apiextensions.k8s.io', 'tenants.kerberator.dell.com') is not None,
                'Tenant CRD exists')
        require(get(a, 'customresourcedefinitions.apiextensions.k8s.io', 'principals.kerberator.dell.com') is not None,
                'Principal CRD exists')
    except (RuntimeError, ValueError) as e:
        require(False, 'cannot inspect prerequisites: ' + str(e))
    for verb, res in [('create', 'tenants.kerberator.dell.com'), ('get', 'tenants.kerberator.dell.com'),
                      ('get', 'daemonsets.apps'), ('get', 'configmaps'), ('get', 'secrets'),
                      ('get', 'roles.rbac.authorization.k8s.io'),
                      ('get', 'rolebindings.rbac.authorization.k8s.io')]:
        ok, detail = auth(a, verb, res)
        require(ok, f'RBAC {verb} {res}' + (f' ({detail})' if not ok else ''))
    try:
        require(get(a, 'tenants.kerberator.dell.com', a.name) is None, 'Tenant name is unused')
    except (RuntimeError, ValueError) as e:
        require(False, 'cannot determine whether Tenant exists: ' + str(e))
    for kind, template in CHILDREN:
        name = template.format(name=a.name)
        try:
            require(get(a, kind, name) is None, f'no existing child collision: {kind}/{name}')
        except (RuntimeError, ValueError) as e:
            require(False, f'cannot inspect {kind}/{name}: {e}')
    # Operator location and node eligibility cannot be established from only this namespace.
    # Ask agent to inspect intended nodes and controller scope read-only; never claim they were checked.
    warn('Operator deployment, reconciliation scope, eligible nodes/taints, image pullability, host-cache overlap and KDC network were NOT verified; inspect before approving creation.')
    print('RESULT: ' + ('BLOCKED' if blocked else 'READY WITH WARNINGS' if warnings else 'READY'))
    print('NOTE: This script does not authorize creation or change Kubernetes resources.')
    return 1 if blocked else 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('name')
    p.add_argument('-n', '--namespace', required=True)
    p.add_argument('--context', required=True)
    p.add_argument('--kubeconfig')
    a = p.parse_args()
    if not shutil.which('kubectl'):
        p.error('kubectl is not installed')
    if not DNS.fullmatch(a.name) or len(a.name) > 40 or not DNS.fullmatch(a.namespace) or len(a.namespace) > 63:
        p.error('invalid name/namespace (Tenant name <=40 chars for generated children)')
    if not a.context or a.context.startswith('-'):
        p.error('explicit --context is required')
    return checks(a)

if __name__ == '__main__':
    sys.exit(main())

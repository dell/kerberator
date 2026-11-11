#!/usr/bin/env python3
"""OP-02: discover configuration and check basic Tenant creation prerequisites.

Read-only. Exit 0 when creation is not blocked (warnings are possible),
1 for a confirmed/uncertain creation blocker, 2 for invalid invocation.
This is NOT a promise that the operator or daemon pods will be Ready.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys

CHILDREN = (
    ('daemonsets.apps', '{name}-kerberator-daemon'),
    ('configmaps', '{name}-roster'),
    ('secrets', '{name}-keytabs'),
    ('roles.rbac.authorization.k8s.io', '{name}-kerberator-daemon-events'),
    ('rolebindings.rbac.authorization.k8s.io', '{name}-kerberator-daemon-events'),
)
DNS = re.compile(r'^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$')
IMAGE_FLAG = re.compile(r'^--daemon-image(?:=(.+))?$')


def run(argv, timeout=25):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, '', str(e)


def kubectl(a, *args, namespaced=True):
    cmd = ['kubectl']
    if a.kubeconfig:
        cmd += ['--kubeconfig', a.kubeconfig]
    if a.context:
        cmd += ['--context', a.context]
    if namespaced and a.namespace:
        cmd += ['-n', a.namespace]
    return run(cmd + list(args))


def read_json(a, *args, namespaced=True):
    rc, out, err = kubectl(a, *args, '-o', 'json', namespaced=namespaced)
    if rc != 0:
        raise RuntimeError(err or out or 'kubectl request failed')
    return json.loads(out)


def object_exists(a, kind, name, namespaced=True):
    # Avoid retrieving Secret contents: existence is tested via resource name only.
    rc, out, err = kubectl(a, 'get', kind, name, '--ignore-not-found',
                           '-o', 'jsonpath={.metadata.name}', namespaced=namespaced)
    if rc:
        raise RuntimeError(err or out or 'resource lookup failed')
    return bool(out)


def can_i(a, verb, resource):
    rc, out, err = kubectl(a, 'auth', 'can-i', verb, resource)
    return rc == 0 and out.strip().lower() == 'yes', err or out


def note(out, level, message):
    out[level.lower()].append(message)


def choose_context(a, result):
    cmd = ['kubectl'] + (['--kubeconfig', a.kubeconfig] if a.kubeconfig else [])
    rc, out, err = run(cmd + ['config', 'get-contexts', '-o', 'name'])
    if rc:
        note(result, 'blocked', 'Cannot read Kubernetes contexts: ' + (err or out))
        return
    contexts = [x.strip() for x in out.splitlines() if x.strip()]
    if a.context and a.context not in contexts:
        note(result, 'blocked', 'Selected context does not exist: ' + a.context)
    elif not a.context:
        if len(contexts) == 1:
            a.context = contexts[0]
        elif not contexts:
            note(result, 'blocked', 'No Kubernetes contexts configured')
        else:
            note(result, 'blocked', 'Several contexts exist; select one with --context: ' + ', '.join(contexts))
    if a.context:
        result['discovered']['context'] = a.context


def choose_namespace(a, result):
    if a.namespace:
        result['discovered']['namespace'] = a.namespace
        return
    # Resolve namespace of selected context; kubectl defaults to "default" when unset.
    rc, out, _ = kubectl(a, 'config', 'view', '--minify',
                         '-o', 'jsonpath={.contexts[0].context.namespace}', namespaced=False)
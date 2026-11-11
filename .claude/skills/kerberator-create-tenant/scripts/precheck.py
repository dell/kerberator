#!/usr/bin/env python3
"""OP-02 read-only discovery and Tenant creation precheck.

No name/realm: discover contexts, candidate namespaces, realms, KDC config,
and images; exit 0 on successful discovery. With a name, run fail-closed
creation prechecks. Nothing in this script writes to the cluster.
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


def execute(args):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, '', str(error)


def kubectl(a, *arguments, namespace=None):
    cmd = ['kubectl']
    if a.kubeconfig:
        cmd.extend(('--kubeconfig', a.kubeconfig))
    if a.context:
        cmd.extend(('--context', a.context))
    if namespace:
        cmd.extend(('-n', namespace))
    return execute(cmd + list(arguments))


def query(a, *arguments, namespace=None):
    rc, out, err = kubectl(a, *arguments, '-o', 'json', namespace=namespace)
    if rc:
        raise RuntimeError(err or out or 'kubectl returned a nonzero status')
    try:
        return json.loads(out)
    except ValueError as error:
        raise RuntimeError('kubectl returned invalid JSON') from error


def exists(a, resource, name, namespace=None):
    # Metadata name only; never retrieve Secret .data or full Secret resources.
    rc, out, err = kubectl(a, 'get', resource, name, '--ignore-not-found',
                           '-o', 'jsonpath={.metadata.name}', namespace=namespace)
    if rc:
        raise RuntimeError(err or out or 'existence lookup failed')
    return bool(out)


def can(a, verb, resource, namespace):
    rc, out, err = kubectl(a, 'auth', 'can-i', verb, resource, namespace=namespace)
    if rc or out.lower() not in ('yes', 'no'):
        raise RuntimeError(err or out or 'authorization check failed')
    return out.lower() == 'yes'


def warning(result, message):
    result['warnings'].append(message)


def block(result, message):
    result['blocked'].append(message)


def unique(items):
    return list(dict.fromkeys(x for x in items if x))


def discover_context(a, result):
    cmd = ['kubectl'] + (['--kubeconfig', a.kubeconfig] if a.kubeconfig else [])
    rc, out, err = execute(cmd + ['config', 'get-contexts', '-o', 'name'])
    if rc:
        block(result, 'Cannot read kubeconfig contexts: ' + (err or out))
        return False
    contexts = unique(out.splitlines())
    result['discovered']['contexts'] = contexts
    if a.context:
        if a.context not in contexts:
            block(result, 'Selected --context is not in this kubeconfig')
            return False
    elif len(contexts) == 1:
        a.context = contexts[0]
    elif len(contexts) > 1:
        result['choices']['contexts'] = contexts
        return False
    else:
        block(result, 'No contexts in kubeconfig')
        return False
    result['selected']['context'] = a.context
    return True


def context_namespace(a):
    # Inspect raw kubeconfig context, NOT kubectl's implicit "default" fallback.
    rc, out, _ = kubectl(a, 'config', 'view', '--minify',
                         '-o', 'jsonpath={.contexts[0].context.namespace}')
    return out if rc == 0 and out and out != 'default' else None


def namespaces(a, result):
    try:
        items = query(a, 'get', 'namespaces')['items']
        result['discovered']['visible_namespaces'] = unique(
            i.get('metadata', {}).get('name') for i in items)
    except (RuntimeError, KeyError, TypeError) as e:
        warning(result, 'Cannot list namespaces: ' + str(e))
    try:
        items = query(a, 'get', 'tenants.kerberator.dell.com', '--all-namespaces')['items']
        result['discovered']['tenant_summaries'] = summarize_tenants(items)
    except (RuntimeError, KeyError, TypeError) as e:
        warning(result, 'Cannot list Tenants across namespaces: ' + str(e))
    names = unique(t['namespace'] for t in result['discovered']['tenant_summaries'])
    hinted = context_namespace(a)
    if hinted:
        names.append(hinted)
    if a.namespace:
        names.insert(0, a.namespace)
    # If no cluster-wide Tenant visibility, still discover existing suitable namespaces.
    if not names:
        names = [n for n in result['discovered']['visible_namespaces']
                 if 'kerberator' in n.lower()]
    names = unique(names)
    eligible = []
    for n in names:
        try:
            if can(a, 'create', 'tenants.kerberator.dell.com', n) and can(a, 'get', 'tenants.kerberator.dell.com', n):
                eligible.append(n)
        except RuntimeError as e:
            warning(result, 'Cannot check Tenant RBAC in ' + n + ': ' + str(e))
    result['discovered']['eligible_namespaces'] = eligible
    if a.namespace:
        result['selected']['namespace'] = a.namespace
        return True
    if len(eligible) == 1:
        a.namespace = eligible[0]
        result['selected']['namespace'] = a.namespace
        return True
    if len(eligible) > 1:
        result['choices']['namespaces'] = eligible
    else:
        warning(result, 'No unique eligible namespace discovered; supply --namespace after inspecting choices/RBAC')
        result['choices']['namespaces'] = names or result['discovered']['visible_namespaces']
    return False


def summarize_tenants(items):
    summaries = []
    for item in items:
        meta, spec = item.get('metadata') or {}, item.get('spec') or {}
        daemon = spec.get('daemon') or {}
        if not isinstance(daemon, dict) or not spec.get('realm'):
            continue
        conf = daemon.get('krb5Conf') or ''
        summaries.append({
            'name': meta.get('name'), 'namespace': meta.get('namespace'),
            'realm': spec['realm'], 'image': daemon.get('image'),
            'host_cache_path': daemon.get('hostCachePath') or '/tmp',
            'has_krb5_conf': bool(conf),
        })
    return summaries


def discover_tenant_config(a, result):
    summaries = result['discovered']['tenant_summaries']
    # Cluster-wide list may be denied. A namespaced list can still work.
    if a.namespace and not any(t['namespace'] == a.namespace for t in summaries):
        try:
            items = query(a, 'get', 'tenants.kerberator.dell.com', namespace=a.namespace)['items']
            summaries.extend(summarize_tenants(items))
        except (RuntimeError, KeyError, TypeError) as e:
            warning(result, 'Cannot list Tenants in selected namespace: ' + str(e))
    realms = unique(t['realm'] for t in summaries)
    result['discovered']['realms'] = realms
    if not a.realm and len(realms) == 1:
        a.realm = realms[0]
    elif not a.realm and len(realms) > 1:
        result['choices']['realms'] = [
            {'realm': realm, 'sources': [t['namespace'] + '/' + t['name'] for t in summaries if t['realm'] == realm]}
            for realm in realms]
    if a.realm:
        result['selected']['realm'] = a.realm
        matches = [t for t in summaries if t['realm'] == a.realm and t['has_krb5_conf']]
        if a.namespace:
            matches.sort(key=lambda t: t['namespace'] != a.namespace)
        result['discovered']['krb5_conf_sources'] = [t['namespace'] + '/' + t['name'] for t in matches]
        # Retrieve only selected realm's non-secret krb5Conf via a targeted field projection.
        for t in matches:
            try:
                rc, out, err = kubectl(a, 'get', 'tenant', t['name'],
                    '-o', 'jsonpath={.spec.daemon.krb5Conf}', namespace=t['namespace'])
                if rc:
                    raise RuntimeError(err or out)
                if '[realms]' in out and a.realm in out:
                    result['discovered']['krb5_conf'] = out
                    result['discovered']['krb5_conf_source'] = t['namespace'] + '/' + t['name']
                    break
            except RuntimeError as e:
                warning(result, 'Cannot read non-secret Kerberos config from ' + t['name'] + ': ' + str(e))
        if not result['discovered'].get('krb5_conf'):
            warning(result, 'No reusable krb5.conf discovered for selected realm; inspect cluster ConfigMaps or KDC Services before creation')


def suggest_name(a, result):
    if a.name or not a.realm or not a.namespace:
        return
    stem = re.sub(r'[^a-z0-9-]+', '-', a.realm.lower()).strip('-').split('-')[0]
    stem = (stem[:27].strip('-') or 'tenant') + '-tenant'
    known = {t['name'] for t in result['discovered']['tenant_summaries']
             if t['namespace'] == a.namespace}
    candidate = stem
    for number in range(2, 101):
        if candidate not in known:
            result['discovered']['suggested_name'] = candidate
            result['discovered']['suggested_name_verified_free'] = False
            # A suggestion is not a collision check; run again with the chosen name.
            return
        candidate = stem + '-' + str(number)
    warning(result, 'Could not suggest an unused Tenant name from known Tenant names')


def discover_images(a, result):
    images = []
    try:
        deployments = query(a, 'get', 'deployments.apps', '--all-namespaces')['items']
        for deployment in deployments:
            meta = deployment.get('metadata') or {}
            for container in (((deployment.get('spec') or {}).get('template') or {}).get('spec') or {}).get('containers', []):
                args = (container.get('command') or []) + (container.get('args') or [])
                if 'kerberator' not in (meta.get('name', '') + ' ' + container.get('image', '')).lower():
                    continue
                for i, arg in enumerate(args):
                    match = IMAGE_FLAG.match(arg)
                    if match:
                        value = match.group(1) or (args[i + 1] if i + 1 < len(args) else '')
                        if value and not value.startswith('-'):
                            images.append({'image': value, 'source': 'operator deployment ' + meta.get('namespace', '') + '/' + meta.get('name', '')})
    except (RuntimeError, KeyError, TypeError) as e:
        warning(result, 'Could not inspect operator deployments: ' + str(e))
    for t in result['discovered']['tenant_summaries']:
        if t['image']:
            images.append({'image': t['image'], 'source': 'Tenant ' + t['namespace'] + '/' + t['name']})
    result['discovered']['daemon_image_candidates'] = images
    distinct = unique(i['image'] for i in images)
    preferred = unique(i['image'] for i in images if i['source'].startswith('operator deployment '))
    if len(preferred) == 1:
        result['discovered']['suggested_daemon_image'] = preferred[0]
    elif not preferred and len(distinct) == 1:
        result['discovered']['suggested_daemon_image'] = distinct[0]
    elif len(distinct) > 1:
        result['choices']['daemon_images'] = distinct
    else:
        warning(result, 'No daemon image discovered from operator or Tenants')


def discover_kdc(a, result):
    try:
        services = query(a, 'get', 'services', '--all-namespaces')['items']
        for service in services:
            meta = service.get('metadata') or {}
            name = meta.get('name', '')
            ports = [p.get('port') for p in (service.get('spec') or {}).get('ports', [])]
            if 'kdc' in name.lower() or 88 in ports:
                result['discovered']['kdc_service_candidates'].append({
                    'name': name, 'namespace': meta.get('namespace'), 'ports': ports})
    except (RuntimeError, KeyError, TypeError) as e:
        warning(result, 'Cannot inspect KDC Services cluster-wide: ' + str(e))


def precheck(a, result):
    if not a.namespace or not a.name:
        return
    try:
        if not exists(a, 'namespaces', a.namespace):
            block(result, 'Namespace does not exist: ' + a.namespace)
    except RuntimeError as e:
        # Namespace get may be denied despite working namespaced API access.
        warning(result, 'Cannot directly inspect Namespace object: ' + str(e))
    for verb in ('create', 'get'):
        try:
            if not can(a, verb, 'tenants.kerberator.dell.com', a.namespace):
                block(result, 'Missing permission: ' + verb + ' tenants in ' + a.namespace)
        except RuntimeError as e:
            block(result, 'Cannot verify Tenant ' + verb + ' permission: ' + str(e))
    for resource, name in [('tenants.kerberator.dell.com', a.name)] + [
        (kind, pattern.format(name=a.name)) for kind, pattern in CHILDREN]:
        try:
            if exists(a, resource, name, namespace=a.namespace):
                block(result, 'Name already exists: ' + resource + '/' + name)
        except RuntimeError as e:
            block(result, 'Cannot rule out name collision for ' + resource + '/' + name + ': ' + str(e))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', nargs='?', help='proposed Tenant name; omit for discovery-only')
    parser.add_argument('--realm', help='selected realm; optional during discovery')
    parser.add_argument('-n', '--namespace', help='target namespace; auto-select only when unambiguous')
    parser.add_argument('--context', help='cluster context; auto-select if only one')
    parser.add_argument('--kubeconfig', help='path to selected kubeconfig')
    parser.add_argument('--json', action='store_true', help='structured discovery/precheck report')
    a = parser.parse_args()
    if not shutil.which('kubectl'):
        parser.error('kubectl is required')
    if a.name and (len(a.name) > 40 or not DNS.fullmatch(a.name)):
        parser.error('Tenant name must be a valid DNS label, at most 40 characters')
    if a.namespace and (len(a.namespace) > 63 or not DNS.fullmatch(a.namespace)):
        parser.error('Namespace must be a valid DNS label')
    result = {
        'status': None, 'blocked': [], 'warnings': [], 'choices': {},
        'selected': {'name': a.name},
        'discovered': {'contexts': [], 'visible_namespaces': [], 'eligible_namespaces': [],
                       'tenant_summaries': [], 'realms': [], 'krb5_conf_sources': [],
                       'daemon_image_candidates': [], 'kdc_service_candidates': []},
    }
    if discover_context(a, result):
        namespaces(a, result)
        discover_tenant_config(a, result)
        suggest_name(a, result)
        discover_images(a, result)
        discover_kdc(a, result)
        if a.name:
            if a.namespace:
                precheck(a, result)
            elif not result['discovered']['eligible_namespaces']:
                block(result, 'No eligible namespace available for creation checks')
        if a.realm and a.realm not in result['discovered']['realms']:
            warning(result, 'Selected realm has no observed existing Tenant; KDC configuration is unverified')
    if result['blocked']:
        result['status'] = 'BLOCKED'
    elif result['choices']:
        result['status'] = 'CHOICE_REQUIRED'
    elif a.name:
        result['status'] = 'READY_WITH_WARNINGS' if result['warnings'] else 'READY'
    else:
        result['status'] = 'DISCOVERED_WITH_WARNINGS' if result['warnings'] else 'DISCOVERED'
    if a.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print('STATUS:', result['status'])
        print('SELECTED:', json.dumps(result['selected'], sort_keys=True))
        print('DISCOVERED:', json.dumps(result['discovered'], sort_keys=True))
        if result['choices']:
            print('CHOICES:', json.dumps(result['choices'], sort_keys=True))
        for msg in result['blocked']:
            print('FAIL:', msg)
        for msg in result['warnings']:
            print('WARN:', msg)
    # Discovery and human choices aren't failures; unsafe name/precheck is.
    return 1 if result['blocked'] else 0


if __name__ == '__main__':
    sys.exit(main())

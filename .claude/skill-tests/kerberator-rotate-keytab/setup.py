#!/usr/bin/env python3
"""OP-04 evaluator fixture preflight and local staging. No cluster mutations."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

NS = 'kerberator-project'
CR = 'uid-2001'
TENANT = 'demo'
KP = 'svc-2001@EXAMPLE.COM'
DECOY = 'svc-2002@EXAMPLE.COM'
FIXTURES = {
    'valid-newer-keytab': ('OP04_SOURCE_NEW', 'svc-2001-new.keytab', KP),
    'lower-kvno-keytab': ('OP04_SOURCE_OLD', 'svc-2001-old.keytab', KP),
    'wrong-principal-keytab': ('OP04_SOURCE_WRONG', 'svc-2002.keytab', DECOY),
    'missing-keytab': None,
    'rotation-applied-remint-timeout': None,
    'mint-failure-after-rotation': None,
}

class SetupError(Exception):
    pass


def fail(message):
    raise SetupError(message)


def info(message):
    print(f'INFO: {message}')


def run(args, error, check=True):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=35)
    except (OSError, subprocess.TimeoutExpired):
        fail(error)
    if check and result.returncode:
        fail(error)
    return result


def setup(fixture, stage=False):
    """Validate fixture prerequisites; optionally stage a local keytab."""
    if fixture not in FIXTURES:
        fail(f'Unknown fixture: {fixture}')
    context = os.environ.get('OP04_CONTEXT')
    if not context:
        fail('Set OP04_CONTEXT to the exact administrator-approved Kubernetes context')
    for tool in ('kubectl', 'git'):
        if not shutil.which(tool):
            fail(f'{tool} is not installed')
    repo = Path(run(['git', 'rev-parse', '--show-toplevel'],
                    'Run within the Kerberator Git repository').stdout.strip())
    if not (repo / '.claude/skills/kerberator-rotate-keytab/SKILL.md').is_file():
        fail('Kerberator OP-04 skill not found in repository')
    current = run(['kubectl', 'config', 'current-context'],
                  'No current Kubernetes context').stdout.strip()
    if current != context:
        fail(f'Context mismatch: current={current!r}, approved={context!r}')

    def kc(*args):
        return run(['kubectl', '--context', context, '-n', NS, *args],
                   f'Cluster read failed: {args[0]} {args[1]}').stdout.strip()

    principal = f'principals.kerberator.dell.com/{CR}'
    kc('get', f'tenants.kerberator.dell.com/{TENANT}', '-o', 'name')
    kc('get', principal, '-o', 'name')
    identity = kc('get', principal, '-o', 'jsonpath={.spec.principal}')
    if identity != KP:
        fail(f'Principal identity mismatch: expected {KP!r}, found {identity!r}')
    ref = kc('get', principal, '-o', 'jsonpath={.spec.keytabSecretRef.name}')
    if not ref:
        fail('Principal has no keytabSecretRef')
    kc('get', f'secret/{ref}', '-o', 'name')
    observed = kc('get', principal, '-o', 'jsonpath={.status.kvnoFromSecret}')
    info(f'Approved context verified: {current}')
    info(f'Namespace={NS} Tenant={TENANT} PrincipalCR={CR} KerberosIdentity={KP}')
    info(f'Current operator-observed KVNO: {observed}')

    if fixture in ('rotation-applied-remint-timeout', 'mint-failure-after-rotation'):
        info('This scenario requires an admin-approved, independently resettable fault condition.')
        fail(f'Fixture {fixture!r} not yet implemented. No change performed.')
    if fixture == 'missing-keytab':
        dest = repo / 'does-not-exist.keytab'
        if os.path.lexists(dest):
            fail(f'The supposed missing keytab already exists: {dest}. Do not delete it automatically.')
        info('PASS: missing path is absent; no file staging required.')
        return 0

    source_var, relative, want = FIXTURES[fixture]
    source_value = os.environ.get(source_var)
    if not source_value:
        fail(f'Set {source_var} to an admin-provided local keytab path')
    source = Path(source_value)
    dest = repo / relative
    if not source.is_file() or not os.access(source, os.R_OK) or source.stat().st_size == 0:
        fail('Keytab source must exist, be readable, and be nonempty')
    if not shutil.which('klist'):
        fail('klist is required for safe principal/KVNO metadata validation')
    entries = run(['klist', '-k', '-t', str(source.resolve())],
                  'Cannot inspect keytab metadata with klist -k -t').stdout
    # Match the exact principal and use only its KVNOs, not unrelated entries.
    versions = []
    for line in entries.splitlines():
        fields = line.split()
        if fields and fields[0].isdigit() and fields[-1] == want:
            versions.append(int(fields[0]))
    if not versions:
        fail(f'Keytab does not contain expected Principal {want} with a valid KVNO')
    key_kvno = max(versions)
    observed = kc('get', principal, '-o', 'jsonpath={.status.kvnoFromSecret}')
    info(f'Validated keytab identity {want!r}, keytab KVNO={key_kvno} (no key bytes printed)')
    if fixture in ('valid-newer-keytab', 'lower-kvno-keytab'):
        if not observed.isdigit():
            fail('Current observed KVNO is unavailable; cannot confirm fixture')
        if fixture == 'valid-newer-keytab' and key_kvno <= int(observed):
            fail('New keytab KVNO must exceed the current Secret KVNO')
        if fixture == 'lower-kvno-keytab' and key_kvno >= int(observed):
            fail('Old keytab KVNO must be below the current Secret KVNO')

    tracked = run(['git', '-C', str(repo), 'ls-files', '--error-unmatch', '--', relative],
                  'Cannot check Git tracking', check=False)
    if tracked.returncode == 0:
        fail(f'Destination is tracked by Git: {relative}')
    if tracked.returncode != 1:
        fail('Cannot establish whether destination is tracked')
    ignored = run(['git', '-C', str(repo), 'check-ignore', '-q', '--', relative],
                  'Cannot check Git ignore rules', check=False)
    if ignored.returncode != 0:
        fail(f'Destination is not confirmed git-ignored: {relative}. Add a safe ignore rule first.')
    if os.path.lexists(dest):
        fail(f'Destination already exists: {dest}. Refusing overwrite; inspect it manually.')
    if not stage:
        info(f'PASS: fixture {fixture!r} is ready to stage at {relative}; no changes made.')
        return 0

    # Exclusive creation refuses existing files and symlinks, with mode 0600.
    with source.open('rb') as src:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            shutil.copyfileobj(src, output)
    info(f'PASS: fixture {fixture!r} staged at {relative} (mode 600, git-ignored).')
    info('No Kubernetes resources or KDC credentials were modified by this script.')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('fixture', choices=FIXTURES)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Validate only (default)')
    mode.add_argument('--stage', action='store_true', help='Stage local keytab without overwriting')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        return setup(args.fixture, stage=args.stage)
    except (SetupError, OSError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

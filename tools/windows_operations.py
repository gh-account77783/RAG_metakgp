"""Stopped native Windows snapshots and reviewable Python-release changes.

No service is started by a recovery or update operation. Protected snapshot
manifests contain paths and ACLs and must stay on the host with the state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time

if __package__ in (None, ''):
    # Isolated installed execution trusts only these administrator-owned siblings.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from tools.windows_service_files import ROLES, path as windows_path, validate_spec
    from tools.windows_installation import no_reparse, reviewed
except ModuleNotFoundError:
    from windows_service_files import ROLES, path as windows_path, validate_spec
    from windows_installation import no_reparse, reviewed


STOP_ORDER = ('worker', 'reader', 'chroma', 'ollama', 'keycloak', 'neo4j', 'postgresql')
UPDATE_ROLES = ('reader', 'worker', 'chroma')
RESERVE = 512 * 1024**2


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def specs_from_review(directory, approved):
    directory = Path(directory)
    manifest = reviewed(directory, approved)
    return {spec['role']: spec for filename in manifest['service_sha256']
            for spec in [json.loads((directory/filename).read_text(encoding='utf-8'))]}


def service_names(specs):
    if set(specs) != set(ROLES):
        raise ValueError('Exactly seven reviewed native services are required')
    for role, spec in specs.items():
        validate_spec(spec)
        if role != spec['role']:
            raise ValueError('Service role does not match its specification')
    bases = {spec['name'][:-len(role)-1] for role, spec in specs.items()}
    if len(bases) != 1:
        raise ValueError('Only one service namespace is allowed')
    return {role: spec['name'] for role, spec in specs.items()}


class SCM:
    """Use numeric SCM state, independent of sc.exe's translated output."""
    def __init__(self):
        import win32api
        import win32con
        import win32security
        import win32service
        self.api = win32service
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
            win32con.TOKEN_ADJUST_PRIVILEGES | win32con.TOKEN_QUERY)
        try:
            privileges = [(win32security.LookupPrivilegeValue(None, name), win32con.SE_PRIVILEGE_ENABLED)
                          for name in ('SeBackupPrivilege', 'SeRestorePrivilege', 'SeSecurityPrivilege')]
            win32security.AdjustTokenPrivileges(token, False, privileges)
            if win32api.GetLastError():
                raise PermissionError('Backup and restore privileges are required')
        finally:
            token.Close()

    def _with_service(self, name, access, action):
        api = self.api
        manager = api.OpenSCManager(None, None, api.SC_MANAGER_CONNECT)
        try:
            handle = api.OpenService(manager, name, access)
            try:
                return action(handle)
            finally:
                api.CloseServiceHandle(handle)
        finally:
            api.CloseServiceHandle(manager)

    def state(self, name):
        return self._with_service(name, self.api.SERVICE_QUERY_STATUS,
                                  lambda h: self.api.QueryServiceStatus(h)[1])

    def clean_stopped(self, name):
        status = self._with_service(name, self.api.SERVICE_QUERY_STATUS, self.api.QueryServiceStatus)
        return status[1] == 1 and status[3] == 0 and status[4] == 0

    def stop(self, name):
        return self._with_service(name, self.api.SERVICE_STOP,
                                  lambda h: self.api.ControlService(h, self.api.SERVICE_CONTROL_STOP))

    def start_type(self, name):
        return self._with_service(name, self.api.SERVICE_QUERY_CONFIG,
                                  lambda h: self.api.QueryServiceConfig(h)[1])

    def disable(self, name):
        api = self.api
        self._with_service(name, api.SERVICE_CHANGE_CONFIG,
            lambda h: api.ChangeServiceConfig(h, api.SERVICE_NO_CHANGE, api.SERVICE_DISABLED,
                api.SERVICE_NO_CHANGE, None, None, 0, None, None, None, None))


def proxy_name(specs, proxy):
    if proxy is not None:
        names = service_names(specs)
        if proxy != names['reader'][:-len('reader')]+'proxy':
            raise ValueError('The optional proxy must use this exact namespace-proxy service name')
    return proxy


def require_stopped(specs, scm, proxy=None):
    names = service_names(specs)
    if any(not scm.clean_stopped(name) for name in names.values()):
        raise ValueError('All seven services must confirm STOPPED with successful exit status')
    if proxy_name(specs, proxy) and (not scm.clean_stopped(proxy) or scm.start_type(proxy) != 4):
        raise ValueError('Public proxy must be STOPPED and DISABLED throughout sealed operations')


def quiesce(specs, scm, proxy=None):
    names = service_names(specs)
    previous_mode = None
    if proxy_name(specs, proxy):
        previous_mode = scm.start_type(proxy)
        scm.disable(proxy)
        if scm.state(proxy) == 4:
            scm.stop(proxy)
        deadline = time.monotonic()+135
        while scm.state(proxy) != 1:
            if scm.state(proxy) != 3 or time.monotonic() >= deadline:
                raise TimeoutError('Public proxy did not confirm STOPPED; application state is untouched')
            time.sleep(.2)
    for role in STOP_ORDER:
        name = names[role]
        state = scm.state(name)
        if state == 1:
            continue
        if state not in (3, 4):  # STOP_PENDING or RUNNING; do not guess at a failed transition.
            raise ValueError('Service is not in a stoppable state')
        if state == 4:
            scm.stop(name)
        deadline = time.monotonic() + specs[role]['stop_timeout_seconds'] + 15
        while scm.state(name) != 1:
            if time.monotonic() >= deadline:
                raise TimeoutError('Service did not confirm STOPPED; no snapshot/update is safe')
            time.sleep(.2)
    require_stopped(specs, scm, proxy)
    return {'services_stopped': 7, 'proxy_previous_start_type': previous_mode, 'proxy_disabled': proxy is not None}


def get_acl(path):
    import win32security as security
    flags = (security.OWNER_SECURITY_INFORMATION | security.GROUP_SECURITY_INFORMATION |
             security.DACL_SECURITY_INFORMATION | security.SACL_SECURITY_INFORMATION)
    descriptor = security.GetFileSecurity(str(path), flags)
    return security.ConvertSecurityDescriptorToStringSecurityDescriptor(descriptor, 1, flags)


def set_acl(path, sddl):
    import win32security as security
    descriptor = security.ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, 1)
    flags = (security.OWNER_SECURITY_INFORMATION | security.GROUP_SECURITY_INFORMATION |
             security.DACL_SECURITY_INFORMATION | security.SACL_SECURITY_INFORMATION)
    control = descriptor.GetSecurityDescriptorControl()[0]
    flags |= (security.PROTECTED_DACL_SECURITY_INFORMATION if control & security.SE_DACL_PROTECTED else
              security.UNPROTECTED_DACL_SECURITY_INFORMATION)
    flags |= (security.PROTECTED_SACL_SECURITY_INFORMATION if control & security.SE_SACL_PROTECTED else
              security.UNPROTECTED_SACL_SECURITY_INFORMATION)
    security.SetFileSecurity(str(path), flags, descriptor)


def private_directory(path):
    Path(path).mkdir(parents=False, exist_ok=False)
    # Snapshot children inherit only SYSTEM and Administrators, never service ACLs.
    set_acl(path, 'O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)')


def reject_alternate_streams(files):
    if os.name != 'nt' or not files:
        return
    # Stream names are omitted by ordinary file hashing. Reject them rather than
    # issuing an apparently complete integrity receipt for unverified bytes.
    script = """$ErrorActionPreference='Stop'
$paths=ConvertFrom-Json ([Console]::In.ReadToEnd())
foreach ($p in $paths) {
    foreach ($s in @(Get-Item -LiteralPath $p -Stream *)) {
        if ($s.Stream -ne ':$DATA') { exit 7 }
    }
}
"""
    result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script],
        input=json.dumps([str(p) for p in files]), text=True, capture_output=True, timeout=120, check=False)
    if result.returncode:
        raise ValueError('Recovery requires ordinary file data streams only')


def inventory(root, *, acls=True):
    root = Path(root)
    no_reparse(root)
    if not root.is_dir():
        raise ValueError('Recovery roots must be existing directories')
    result = {}; regular_files = []
    for directory, directories, files in os.walk(root, followlinks=False):
        for item in [Path(directory), *(Path(directory)/name for name in files)]:
            no_reparse(item)
            info = item.stat()
            relative = item.relative_to(root).as_posix()
            record = {'kind': 'directory' if item.is_dir() else 'file', 'mtime_ns': info.st_mtime_ns}
            if record['kind'] == 'file':
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('Only ordinary unlinked files may be snapshotted')
                record.update(size=info.st_size, sha256=digest(item))
                regular_files.append(item)
            if acls:
                record['acl'] = get_acl(item)
            result[relative] = record
        for name in directories:
            no_reparse(Path(directory)/name)
    reject_alternate_streams(regular_files)
    return result


def contents(records):
    return {name: {k: v for k, v in value.items() if k != 'acl'} for name, value in records.items()}


def copy_tree(source, target):
    # /COPY:DAT deliberately avoids installing service ACLs inside private backups.
    # Never use /MIR or /PURGE: retained trees and failed stages are evidence.
    result = subprocess.run(['robocopy.exe', str(source), str(target), '/E', '/COPY:DAT',
        '/DCOPY:DAT', '/XJ', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NP'],
        capture_output=True, check=False)
    if result.returncode >= 8:
        raise OSError('Native state copy failed; preserve its partial private directory')


def validate_roots(specs, roots, destination):
    service_names(specs)
    if not isinstance(roots, dict) or not {'application', 'configuration', 'postgresql', 'keycloak', 'neo4j'} <= set(roots):
        raise ValueError('Explicit application/configuration and all backend roots are required')
    if any(not re.fullmatch('[a-z][a-z0-9_]{0,31}', key) for key in roots):
        raise ValueError('Recovery root labels must be simple identifiers')
    paths = [Path(value).absolute() for value in roots.values()]
    if any(str(p) != str(roots[k]) or p == Path(p.anchor) for k, p in zip(roots, paths)):
        raise ValueError('Recovery roots must be canonical absolute paths below a drive root')
    target = Path(destination).absolute()
    combined = [*paths, target]
    if any(a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(combined) for b in combined[i+1:]):
        raise ValueError('Snapshot, restore and state roots must not overlap')
    for item in combined:
        no_reparse(item)
    # A snapshot must not accidentally absorb a release or the model weight store.
    excluded = [Path(spec['command'][0]).parent for spec in specs.values()]
    model = specs['ollama']['environment'].get('OLLAMA_MODELS')
    if model:
        excluded.append(Path(model))
    if any(a.is_relative_to(b) or b.is_relative_to(a) for a in paths for b in excluded):
        raise ValueError('State snapshots exclude software releases and model weights')
    required = [Path(spec['state_dir']) for spec in specs.values()]
    required += [Path(spec['environment_file']) for spec in specs.values() if spec['environment_file']]
    for role in ('reader', 'worker'):
        command = specs[role]['command']
        required += [Path(command[index+1]) for index, value in enumerate(command[:-1])
                     if value in ('--config', '--dotenv', '--ssl-keyfile', '--ssl-certfile')]
    if any(not any(item.is_relative_to(root) for root in paths) for item in required):
        raise ValueError('Scope must include every service state and protected configuration input')


def backup(specs, roots, output, scm, proxy=None):
    output = Path(output)
    validate_roots(specs, roots, output)
    require_stopped(specs, scm, proxy)
    before = {name: inventory(value) for name, value in roots.items()}
    total = sum(item.get('size', 0) for records in before.values() for item in records.values())
    if shutil.disk_usage(output.parent).free < total + RESERVE:
        raise ValueError('Insufficient backup capacity plus the 512 MiB reserve')
    private_directory(output)
    for name, value in roots.items():
        copy_tree(value, output/name)
        if inventory(output/name, acls=False) != contents(before[name]):
            raise ValueError('Snapshot copied contents do not match the stopped source')
    require_stopped(specs, scm, proxy)
    if before != {name: inventory(value) for name, value in roots.items()}:
        raise ValueError('Source changed while snapshotting; do not use this partial snapshot')
    manifest = {'schema_version': 1, 'services': service_names(specs), 'roots': roots, 'inventory': before}
    (output/'snapshot.json').write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
    return {'snapshot_sha256': digest(output/'snapshot.json'), 'files_bytes': total, 'services_started': False}


def restore(specs, snapshot, approved, seal, scm, proxy=None):
    snapshot, seal = Path(snapshot), Path(seal)
    if digest(snapshot/'snapshot.json') != approved:
        raise ValueError('Snapshot differs from the retained reviewed SHA256')
    manifest = json.loads((snapshot/'snapshot.json').read_text(encoding='utf-8'))
    if manifest['schema_version'] != 1 or manifest['services'] != service_names(specs):
        raise ValueError('Snapshot belongs to a different service namespace')
    if seal.name != 'RESTORE_SEALED.json':
        raise ValueError('Use the service host RESTORE_SEALED.json exposure guard')
    roots = manifest['roots']
    validate_roots(specs, roots, snapshot)
    if any(seal.is_relative_to(Path(value)) for value in roots.values()):
        raise ValueError('The stable service-spec exposure seal must remain outside replaced state/configuration roots')
    require_stopped(specs, scm, proxy)
    stages = {}
    for name, target in roots.items():
        records = manifest['inventory'][name]
        if inventory(snapshot/name, acls=False) != contents(records):
            raise ValueError('Snapshot content verification failed before any state replacement')
        target = Path(target)
        stage, retained = target.with_name(target.name+'.restore-stage'), target.with_name(target.name+'.pre-restore')
        if stage.exists() or retained.exists():
            raise FileExistsError('Retained pre-restore/partial stage exists; inspect before another restore')
        no_reparse(stage); no_reparse(retained)
        stages[name] = (stage, retained)
    # Capacity is aggregated per filesystem before writing any stage.
    volumes = {}
    for name, (stage, _) in stages.items():
        device = stage.parent.stat().st_dev
        previous, sample = volumes.get(device, (0, stage.parent))
        volumes[device] = (previous+sum(v.get('size', 0) for v in manifest['inventory'][name].values()), sample)
    if any(shutil.disk_usage(sample).free < size+RESERVE for size, sample in volumes.values()):
        raise ValueError('Insufficient restore-stage capacity plus the 512 MiB reserve')
    no_reparse(seal.parent)
    # Exclusive seal survives failure and explicitly blocks any unattended exposure.
    with seal.open('x', encoding='utf-8') as stream:
        json.dump({'snapshot_sha256': approved, 'reader_worker_must_remain_stopped': True,
                   'latest_account_and_tombstone_reconciliation_required': True}, stream)
    for name, (stage, _) in stages.items():
        private_directory(stage)
        copy_tree(snapshot/name, stage)
        if inventory(stage, acls=False) != contents(manifest['inventory'][name]):
            raise ValueError('Restore scratch verification failed; no service may be exposed')
        # Restore parent ACLs first; children then regain their exact explicit ACLs.
        for relative, record in sorted(manifest['inventory'][name].items(), key=lambda item: len(Path(item[0]).parts)):
            set_acl(stage/relative, record['acl'])
        if inventory(stage) != manifest['inventory'][name]:
            raise ValueError('Restored credential/state ACL verification failed')
    require_stopped(specs, scm, proxy)
    for name, (stage, retained) in stages.items():
        target = Path(roots[name])
        target.rename(retained)
        stage.rename(target)
    return {'restored_roots': len(roots), 'sealed': True, 'services_started': False}


def prepare_update(previous, candidate, output):
    """Only three executable paths change; state/configuration/backends stay exact."""
    service_names(previous); service_names(candidate)
    if service_names(previous) != service_names(candidate):
        raise ValueError('Release update must retain the same namespace')
    for role in ROLES:
        old, new = previous[role], candidate[role]
        expected = {**old, 'command': [new['command'][0], *old['command'][1:]]} if role in UPDATE_ROLES else old
        if new != expected or (role in UPDATE_ROLES and new['command'][0] == old['command'][0]):
            raise ValueError('Update may change only the three Python executable paths')
    old_bin = windows_path(previous['reader']['command'][0]).parent
    new_bin = windows_path(candidate['reader']['command'][0]).parent
    if (candidate['reader']['command'][0] != candidate['worker']['command'][0]
            or windows_path(candidate['chroma']['command'][0]).parent != new_bin
            or old_bin.parent.is_relative_to(new_bin.parent) or new_bin.parent.is_relative_to(old_bin.parent)):
        raise ValueError('Use separate immutable old/new Python closures')
    # Reuse path overlap checks in the complete renderer during candidate preparation.
    output = Path(output)
    output.mkdir(parents=False, exist_ok=False)
    manifest = {'schema_version': 1, 'services': service_names(previous), 'before': {}, 'after': {}}
    for phase, specs in [('before', previous), ('after', candidate)]:
        (output/phase).mkdir()
        for role in UPDATE_ROLES:
            filename = specs[role]['name']+'.json'
            target = output/phase/filename
            target.write_text(json.dumps(specs[role], indent=2)+'\n', encoding='utf-8')
            manifest[phase][filename] = digest(target)
    (output/'update.json').write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
    return {'update_sha256': digest(output/'update.json'), 'services_started': False}


def apply_update(specs, prepared, approved, live, scm, *, rollback=False, proxy=None):
    prepared, live = Path(prepared), Path(live)
    if digest(prepared/'update.json') != approved:
        raise ValueError('Release plan differs from reviewed SHA256')
    manifest = json.loads((prepared/'update.json').read_text(encoding='utf-8'))
    if manifest['schema_version'] != 1 or manifest['services'] != service_names(specs):
        raise ValueError('Update belongs to a different service namespace')
    expected = {specs[role]['name']+'.json' for role in UPDATE_ROLES}
    if set(manifest['before']) != expected or set(manifest['after']) != expected:
        raise ValueError('Update must contain exactly three Python service specifications')
    source, target = ('after', 'before') if rollback else ('before', 'after')
    for filename in expected:
        for phase in ('before', 'after'):
            if digest(prepared/phase/filename) != manifest[phase][filename]:
                raise ValueError('Reviewed immutable release specification changed')
        if digest(live/filename) != manifest[source][filename]:
            raise ValueError('Live service specification drift; preserve and inspect before switching')
        no_reparse(live/filename)
    require_stopped(specs, scm, proxy)
    for filename in expected:
        stage = live/(filename+'.pending')
        with stage.open('xb') as stream:
            stream.write((prepared/target/filename).read_bytes())
        set_acl(stage, get_acl(live/filename))
    require_stopped(specs, scm, proxy)
    for filename in expected:
        os.replace(live/(filename+'.pending'), live/filename)
    return {'specifications_changed': 3, 'rollback': rollback, 'services_started': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('stop', 'backup', 'restore', 'prepare-update', 'update', 'rollback'))
    parser.add_argument('reviewed', type=Path)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--candidate-sha256')
    parser.add_argument('--roots', type=Path)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--sha256')
    parser.add_argument('--live-specs', type=Path)
    parser.add_argument('--seal', type=Path)
    parser.add_argument('--proxy-service', help='Optional exact namespace-proxy native SCM service; keep disabled until reconciliation')
    args = parser.parse_args()
    try:
        specs = specs_from_review(args.reviewed, args.manifest_sha256)
        if args.command == 'prepare-update':
            if not all((args.candidate, args.candidate_sha256, args.directory)):
                parser.error('prepare-update needs candidate, candidate-sha256 and directory')
            result = prepare_update(specs, specs_from_review(args.candidate, args.candidate_sha256), args.directory)
        else:
            if os.name != 'nt':
                parser.error('Native Windows with the reviewed pywin32 service host is required')
            scm = SCM()
            if args.command == 'stop':
                result = quiesce(specs, scm, args.proxy_service)
            elif args.command == 'backup':
                if not args.roots or not args.directory:
                    parser.error('backup needs roots and directory')
                result = backup(specs, json.loads(args.roots.read_text(encoding='utf-8')), args.directory, scm, args.proxy_service)
            elif args.command == 'restore':
                if not all((args.directory, args.sha256, args.seal)):
                    parser.error('restore needs directory, sha256 and seal')
                result = restore(specs, args.directory, args.sha256, args.seal, scm, args.proxy_service)
            else:
                if not all((args.directory, args.sha256, args.live_specs)):
                    parser.error('update/rollback needs directory, sha256 and live-specs')
                result = apply_update(specs, args.directory, args.sha256, args.live_specs, scm,
                                      rollback=args.command == 'rollback', proxy=args.proxy_service)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, KeyError, TypeError, TimeoutError):
        parser.exit(2, 'Native operation incomplete; preserve private partial files and stopped services for inspection\n')


if __name__ == '__main__':
    main()

"""Verify native Windows service files and install an unused SCM namespace, stopped."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys

if __package__ in (None, ''):
    # Isolated Python omits the script directory; trust only these retained siblings.
    sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tools.windows_service_files import ROLES, namespace, validate_spec, path
except ModuleNotFoundError:
    from windows_service_files import ROLES, namespace, validate_spec, path


def reviewed(directory, approved):
    directory = Path(directory)
    no_reparse(directory)
    source = directory/'installation.json'
    if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != approved:
        raise ValueError('Installation manifest differs from the separately reviewed SHA256')
    manifest = json.loads(source.read_text(encoding='utf-8'))
    if (set(manifest) != {'schema_version', 'name', 'service_sha256', 'layout_sha256'}
            or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1):
        raise ValueError('Invalid installation manifest')
    name = namespace(manifest['name'])
    expected = {name + '-' + role + '.json' for role in ROLES}
    if set(manifest['service_sha256']) != expected:
        raise ValueError('Expected exactly seven native service specifications')
    for filename, digest in manifest['service_sha256'].items():
        target = directory/filename
        if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError('Reviewed service bytes changed')
        spec = validate_spec(json.loads(target.read_text(encoding='utf-8')))
        if filename != spec['name'] + '.json':
            raise ValueError('Service name differs from its reviewed filename')
    return manifest


def no_reparse(target):
    """Do not install/review through Windows links or junctions, including ancestors."""
    target = Path(target).absolute()
    for candidate in (target, *target.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if candidate.is_symlink() or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024):
            raise ValueError('Windows service paths must not traverse reparse points')


def service_command(host_python, host_script, spec_path):
    for item in (host_python, host_script, spec_path):
        path(str(item))
    return subprocess.list2cmdline([str(host_python), '-I', '-B', str(host_script), str(spec_path)])


def install_new(directory, manifest, *, live_spec_dir, host_python, host_script):
    """Create stopped virtual-account services; preserve any partial installation for inspection."""
    if sys.platform != 'win32':
        raise ValueError('Native Windows is required')
    import win32service
    import pywintypes
    import win32security
    import ntsecuritycon

    def protect_created(target, readers=()):
        acl = win32security.ACL()
        for sid_text in ('S-1-5-18', 'S-1-5-32-544'):  # SYSTEM and local Administrators.
            acl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, 0,
                ntsecuritycon.FILE_ALL_ACCESS, win32security.ConvertStringSidToSid(sid_text))
        for sid in readers:
            acl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, 0,
                ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_EXECUTE, sid)
        win32security.SetNamedSecurityInfo(str(target), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)

    for item in (directory, live_spec_dir, host_python, host_script):
        no_reparse(item)
    directory, live_spec_dir = Path(directory).absolute(), Path(live_spec_dir).absolute()
    host_python, host_script = Path(host_python).absolute(), Path(host_script).absolute()
    path(str(live_spec_dir)); path(str(host_python)); path(str(host_script))
    if live_spec_dir == directory:
        raise ValueError('Keep reviewed immutable specifications separate from active service files')
    if not host_python.is_file() or not host_script.is_file():
        raise ValueError('Stable maintenance interpreter and host script must already exist')
    schema_script = host_script.with_name('windows_service_files.py')
    if not schema_script.is_file():
        raise ValueError('Stable host requires its reviewed sibling service-schema script')
    for filename in manifest['service_sha256']:
        spec = json.loads((directory/filename).read_text(encoding='utf-8'))
        if Path(spec['command'][0]).resolve() == host_python:
            raise ValueError('SCM host interpreter must be separate from replaceable application releases')
    manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
    try:
        # Check the entire namespace before creating a directory, file, or SCM object.
        for role in ROLES:
            name = manifest['name'] + '-' + role
            try:
                service = win32service.OpenService(manager, name, win32service.SERVICE_QUERY_CONFIG)
            except pywintypes.error as exc:
                if exc.winerror != 1060:
                    raise
            else:
                win32service.CloseServiceHandle(service)
                raise ValueError('Installation refuses an existing SCM service namespace')
        if live_spec_dir.exists():
            raise ValueError('Installation refuses an existing active specification directory')
        live_spec_dir.mkdir(parents=True, exist_ok=False)
        protect_created(live_spec_dir)
        for filename, digest in manifest['service_sha256'].items():
            contents = (directory/filename).read_bytes()
            if hashlib.sha256(contents).hexdigest() != digest:
                raise ValueError('Reviewed bytes changed before installation')
            with (live_spec_dir/filename).open('xb') as stream:
                stream.write(contents)
            protect_created(live_spec_dir/filename)
        installed = []
        readers = []
        for role in ROLES:
            name = manifest['name'] + '-' + role
            spec = json.loads((live_spec_dir/(name + '.json')).read_text(encoding='utf-8'))
            service = win32service.CreateService(manager, name, name,
                win32service.SERVICE_ALL_ACCESS, win32service.SERVICE_WIN32_OWN_PROCESS,
                win32service.SERVICE_DEMAND_START, win32service.SERVICE_ERROR_NORMAL,
                service_command(host_python, host_script, live_spec_dir/(name + '.json')),
                None, 0, spec['dependencies'], 'NT SERVICE\\' + name, None)
            try:
                win32service.ChangeServiceConfig2(service, win32service.SERVICE_CONFIG_SERVICE_SID_INFO,
                                                 win32service.SERVICE_SID_TYPE_UNRESTRICTED)
                win32service.ChangeServiceConfig2(service, win32service.SERVICE_CONFIG_DESCRIPTION,
                                                 'Isolated native GraphMind ' + role)
            finally:
                win32service.CloseServiceHandle(service)
            sid = win32security.LookupAccountName(None, 'NT SERVICE\\' + name)[0]
            readers.append(sid)
            protect_created(live_spec_dir/(name + '.json'), (sid,))
            protect_created(live_spec_dir, readers)
            installed.append(name)
        receipt = {'schema_version': 1, 'services_installed': installed, 'services_activated': False,
                   'start_mode': 'manual', 'live_spec_dir': str(live_spec_dir),
                   'host_python': str(host_python), 'host_script': str(host_script),
                   'host_sha256': hashlib.sha256(host_script.read_bytes()).hexdigest(),
                   'schema_sha256': hashlib.sha256(schema_script.read_bytes()).hexdigest(),
                   'host_python_sha256': hashlib.sha256(host_python.read_bytes()).hexdigest()}
        with (directory/'installed.json').open('x', encoding='utf-8') as stream:
            json.dump(receipt, stream, indent=2)
            stream.write('\n')
        return receipt
    finally:
        win32service.CloseServiceHandle(manager)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('verify', 'install'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--live-spec-dir', type=Path)
    parser.add_argument('--host-python', type=Path)
    parser.add_argument('--host-script', type=Path)
    args = parser.parse_args()
    manifest = reviewed(args.directory, args.manifest_sha256)
    if args.command == 'install':
        if not all((args.live_spec_dir, args.host_python, args.host_script)):
            parser.error('install requires --live-spec-dir, --host-python and --host-script')
        result = install_new(args.directory, manifest, live_spec_dir=args.live_spec_dir,
                             host_python=args.host_python, host_script=args.host_script)
    else:
        result = {'reviewed': True, 'name': manifest['name'], 'services_activated': False}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

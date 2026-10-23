"""Verify an approved native bundle and install offline using only Python's standard library."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess
import sys
import sysconfig
import venv


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def native_target() -> dict[str, str]:
    return {'implementation': platform.python_implementation(),
            'python': f'{sys.version_info.major}.{sys.version_info.minor}',
            'platform': sysconfig.get_platform()}


def pip_environment() -> dict[str, str]:
    environment = {key:value for key,value in os.environ.items()
                   if key not in {'PYTHONPATH','PYTHONHOME','PYTHONUSERBASE'}}
    environment['PYTHONNOUSERSITE'] = '1'
    return environment


def lock_text(records: list[dict]) -> str:
    return ''.join(f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}\n"
                   for item in records)


def reject_revoked_wheel(name: str, version: str) -> None:
    # Upstream incident: https://docs.litellm.ai/blog/security-incident-oct-21-2026
    # A previously approved hash cannot make withdrawn malicious bytes usable.
    if name == 'litellm' and version.split('+', 1)[0] == '1.107.0':
        raise ValueError('Revoked compromised distribution: litellm 1.107.0')


def bundle_file(bundle: Path, relative: str) -> Path:
    if not isinstance(relative,str) or any(ord(c)<32 or c in '\\:' for c in relative):
        raise ValueError('Unsafe bundle path')
    path = PurePosixPath(relative)
    if path.is_absolute() or str(path)!=relative or '..' in path.parts or not path.parts:
        raise ValueError('Unsafe bundle path')
    target = bundle.joinpath(*path.parts)
    if (not target.is_file() or not target.resolve().is_relative_to(bundle)
            or any(bundle.joinpath(*path.parts[:n]).is_symlink() for n in range(1,len(path.parts)+1))):
        raise ValueError('Bundle content must be retained regular files')
    return target


def verify_installer(bundle: Path, manifest: dict) -> None:
    # Keep pre-bootstrap schema_version1 bundles usable through the build tool.
    if 'installer' not in manifest:
        return
    record = manifest['installer']
    if record['filename']!='install_bundle.py' or digest(bundle_file(bundle,record['filename']))!=record['sha256']:
        raise ValueError('Bundle installer bytes changed')


def verify_notices(bundle: Path, manifest: dict) -> None:
    for relative,expected in manifest['notices'].items():
        if not relative.startswith('notices/') or digest(bundle_file(bundle,relative))!=expected:
            raise ValueError('Upstream notice bytes changed')
    actual={p.relative_to(bundle).as_posix() for p in (bundle/'notices').rglob('*')
            if p.is_file() or p.is_symlink()}
    if actual!=set(manifest['notices']):
        raise ValueError('Notice inventory changed')
    wheels={item['name']:item for item in manifest['wheels']}
    paths=set()
    for item in manifest.get('supplemental_notices',[]):
        wheel=wheels.get(item['distribution'])
        if (not wheel or item['version']!=wheel['version'] or item['wheel_sha256']!=wheel['sha256']
                or item['path'] in paths or item['path'] not in manifest['notices']
                or not item['path'].startswith('notices/'+item['distribution']+'/supplemental/')
                or item['sha256']!=manifest['notices'][item['path']]
                or not isinstance(item['source_url'],str) or not item['source_url'].startswith('https://')
                or not isinstance(item['revision'],str) or not item['revision'].strip()):
            raise ValueError('Supplemental notice provenance differs from the pinned wheel')
        paths.add(item['path'])


def verify_approved(bundle: Path, manifest_sha256: str) -> dict:
    """Check an externally approved manifest without importing packaging or resolving code."""
    if not isinstance(manifest_sha256,str) or not re.fullmatch(r'[0-9a-fA-F]{64}',manifest_sha256):
        raise ValueError('Supply the separately approved manifest SHA256')
    if bundle.is_symlink():
        raise ValueError('Bundle directory cannot be a symlink')
    bundle = bundle.resolve()
    path = bundle_file(bundle,'manifest.json')
    if digest(path)!=manifest_sha256.lower():
        raise ValueError('Bundle manifest does not match the approved SHA256')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('schema_version')!=1 or manifest.get('target')!=native_target():
        raise ValueError('Bundle schema or native target does not match')
    records = manifest['wheels']
    if not isinstance(records,list) or not records:
        raise ValueError('Bundle wheel inventory is empty')
    filenames=set();names=set()
    for item in records:
        if (not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',item['name'])
                or not re.fullmatch(r'[A-Za-z0-9.!+_-]+',item['version'])
                or not re.fullmatch(r'[0-9a-f]{64}',item['sha256'])
                or type(item['size']) is not int or item['size']<=0):
            raise ValueError('Invalid pinned wheel identity')
        filename=item['filename']
        reject_revoked_wheel(item['name'], item['version'])
        if (not isinstance(filename,str) or PurePosixPath(filename).name!=filename
                or not filename.endswith('.whl') or filename in filenames or item['name'] in names):
            raise ValueError('Bundle needs one wheel per distribution')
        path=bundle_file(bundle,'wheelhouse/'+filename)
        if path.stat().st_size!=item['size'] or digest(path)!=item['sha256']:
            raise ValueError('Wheel inventory or bytes changed')
        filenames.add(filename);names.add(item['name'])
    if 'graphmind-rag-mcp' not in names or {p.name for p in (bundle/'wheelhouse').iterdir()}!=filenames:
        raise ValueError('Wheel inventory is incomplete or contains extra files')
    lock=bundle_file(bundle,'requirements.lock')
    if digest(lock)!=manifest['lock_sha256'] or lock.read_bytes()!=lock_text(records).encode('utf-8'):
        raise ValueError('Dependency lock differs from the wheel inventory')
    verify_notices(bundle,manifest)
    verify_installer(bundle,manifest)
    return manifest


def install_verified(bundle: Path, environment: Path, manifest: dict) -> dict:
    """Shared installer for verified build-tool and standalone bootstrap entrypoints."""
    for item in manifest['wheels']:
        reject_revoked_wheel(item['name'], item['version'])
    bundle, environment = bundle.resolve(),environment.resolve()
    if environment.exists() or environment.is_relative_to(bundle) or bundle.is_relative_to(environment):
        raise ValueError('Installation requires a new environment outside the bundle')
    environment.parent.mkdir(parents=True,exist_ok=True)
    environment.mkdir()
    # Native Unix environments retain the qualified base runtime; avoid three
    # redundant standalone-interpreter copies. Windows keeps ordinary copies.
    venv.EnvBuilder(with_pip=True,symlinks=sys.platform!='win32').create(environment)
    executable=environment/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
    with (environment/'bundle-install.log').open('wb') as log:
        subprocess.run([str(executable),'-m','pip','--isolated','--disable-pip-version-check',
            'install','--no-index','--no-compile','--only-binary=:all:','--find-links',str(bundle/'wheelhouse'),
            '--require-hashes','-r',str(bundle/'requirements.lock')],stdout=log,stderr=subprocess.STDOUT,
            check=True,env=pip_environment())
        subprocess.run([str(executable),'-m','pip','--isolated','check'],stdout=log,stderr=subprocess.STDOUT,
            check=True,env=pip_environment())
    receipt={'manifest_sha256':digest(bundle/'manifest.json'),'target':manifest['target'],
        'wheels':len(manifest['wheels']),'offline_install':True,'pip_check':True}
    (environment/'bundle-install.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle',type=Path)
    parser.add_argument('--manifest-sha256',required=True,help='Approved release hash from outside this bundle')
    parser.add_argument('--environment',type=Path,help='New immutable environment, outside the bundle')
    parser.add_argument('--verify-only',action='store_true')
    args=parser.parse_args()
    if args.verify_only == bool(args.environment):
        parser.error('Choose --verify-only or --environment')
    try:
        manifest=verify_approved(args.bundle,args.manifest_sha256)
        result=({'verified':True,'target':manifest['target'],'wheels':len(manifest['wheels']),
                 'manifest_sha256':args.manifest_sha256.lower(),'rights_disposition':manifest['rights_disposition']}
                if args.verify_only else install_verified(args.bundle,args.environment,manifest))
    except (OSError,ValueError,KeyError,TypeError,subprocess.CalledProcessError) as exc:
        parser.exit(2,f'Bundle bootstrap failed ({type(exc).__name__}); partial environment/logs retained\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()

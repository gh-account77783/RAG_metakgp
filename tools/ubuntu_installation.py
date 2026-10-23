"""Prepare and preflight a complete native service layout; install reviewed new units only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys

from tools import ubuntu_backend_files as backends
from tools import ubuntu_service_files as application


ROLES=(*backends.ROLES,*application.ROLES)


def complete_layout(app, backend):
    units=application.render(app) | backends.render(backend)
    if app['name']!=backend['name']:
        raise ValueError('Application and backends need one service namespace')
    if app['user'] in {backend[role]['user'] for role in backends.ROLES}:
        raise ValueError('Application and backends need distinct service accounts')
    paths=[PurePosixPath(app[key]) for key in ('data_dir','model_dir','ollama_home')]
    paths += [PurePosixPath(backend[role]['state_dir']) for role in backends.ROLES]
    if any(a.is_relative_to(b) or b.is_relative_to(a)
           for i,a in enumerate(paths) for b in paths[i+1:]):
        raise ValueError('Application, models and backend state must not overlap')
    release=PurePosixPath(app['python']).parent
    if release!=PurePosixPath(app['chroma']).parent:
        raise ValueError('Python and Chroma must use the same immutable release')
    dependencies=' '.join(app['name']+'-'+role+'.service' for role in backends.ROLES)
    for role in ('reader','worker'):
        name=app['name']+'-'+role+'.service'
        units[name]=units[name].replace('[Service]', 'After='+dependencies+'\nWants='+dependencies+'\n\n[Service]',1)
    return units


def requirements(app, backend):
    """Paths only: use existing app/configuration commands to validate configuration values."""
    result=[]
    def add(user,path,access,mutable=False):
        result.append({'user':user,'path':str(path),'access':access,'mutable':mutable})
    user=app['user'];data=PurePosixPath(app['data_dir'])
    for key in ('python','chroma','ollama'):add(user,app[key],'x')
    for key in ('config','dotenv','model_dir'):add(user,app[key],'r')
    for key in application.TLS_FIELDS:
        if key in app:add(user,app[key],'r')
    for path in (data,data/'chroma',data/'home',data/'cache',PurePosixPath(app['ollama_home'])):
        add(user,path,'w',True)
    for role in backends.ROLES:
        spec=backend[role];user=spec['user'];add(user,spec['state_dir'],'w',True)
        if role=='postgresql':
            add(user,spec['binary'],'x');add(user,spec['library_dir'],'r')
            add(user,spec['data_dir'],'w',True)
        else:
            add(user,PurePosixPath(spec['java_home'])/'bin/java','x')
            add(user,PurePosixPath(spec['home'])/('bin/kc.sh' if role=='keycloak' else 'bin/neo4j'),'x')
            if role=='neo4j':add(user,spec['config_dir'],'r')
    return result


def prepare(app, backend, output):
    units=complete_layout(app,backend)
    application.write_units(units,output)
    manifest={'schema_version':1,'name':app['name'],'unit_sha256':{
        name:hashlib.sha256((output/name).read_bytes()).hexdigest() for name in units},
        'requirements':requirements(app,backend),
        'protected_files':[app['config'],app['dotenv'],backend['keycloak']['environment_file'],
                           *([app['ssl_keyfile']] if 'ssl_keyfile' in app else [])],
        'data_dir':app['data_dir'],'accounts':[app['user'],*[backend[r]['user'] for r in backends.ROLES]],
        'layout_sha256':hashlib.sha256(json.dumps([app,backend],sort_keys=True).encode()).hexdigest()}
    (output/'installation.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    return manifest


def reviewed(directory,approved):
    path=directory/'installation.json'
    if hashlib.sha256(path.read_bytes()).hexdigest()!=approved:
        raise ValueError('Installation manifest does not match the separately reviewed SHA256')
    manifest=json.loads(path.read_text(encoding='utf-8'))
    name=manifest['name']
    # Reuse the renderer namespace validation without accepting arbitrary unit paths.
    import re
    if manifest['schema_version']!=1 or not re.fullmatch(r'graphmind-[a-z0-9][a-z0-9-]{0,39}',name):
        raise ValueError('Invalid installation namespace')
    expected={name+'-'+role+'.service' for role in ROLES}
    if set(manifest['unit_sha256'])!=expected:
        raise ValueError('Expected exactly seven native units')
    for filename,digest in manifest['unit_sha256'].items():
        unit=directory/filename
        if unit.is_symlink() or hashlib.sha256(unit.read_bytes()).hexdigest()!=digest:
            raise ValueError('Reviewed unit bytes changed')
    return manifest


def preflight(manifest):
    """Inspect effective service-user access without reading protected file contents."""
    import pwd
    identifiers=[pwd.getpwnam(user).pw_uid for user in manifest['accounts']]
    if 0 in identifiers or len(set(identifiers))!=len(identifiers):
        raise ValueError('Service accounts must resolve to distinct non-root UIDs')
    failures=[]
    for item in manifest['requirements']:
        command=['runuser','-u',item['user'],'--','test']
        if subprocess.run([*command,'-'+item['access'],item['path']],capture_output=True).returncode:
            failures.append({'path':item['path'],'user':item['user'],'reason':'required_access_missing'})
        elif not item['mutable'] and subprocess.run([*command,'-w',item['path']],capture_output=True).returncode==0:
            failures.append({'path':item['path'],'user':item['user'],'reason':'immutable_input_is_writable'})
    for value in manifest['protected_files']:
        path=Path(value)
        if not path.is_file():
            failures.append({'path':value,'reason':'protected_file_missing'})
        else:
            mode=path.stat()
            if mode.st_uid!=0 or mode.st_mode & 0o027:
                failures.append({'path':value,'reason':'protected_file_permissions'})
    free=shutil.disk_usage(manifest['data_dir']).free
    if free<512*1024**2:failures.append({'reason':'less_than_512_MiB_free'})
    return {'preflight_passed':not failures,'free_bytes':free,'failures':failures,
            'configuration_values_checked':False,'services_activated':False}


def install_new(directory,manifest,unit_dir=Path('/etc/systemd/system')):
    # Existing namespaces, vendor units and transient units belong to update/recovery.
    for name in manifest['unit_sha256']:
        if any(os.path.lexists(root/name) or os.path.lexists(root/(name+'.d'))
               for root in (unit_dir,Path('/run/systemd/system'),Path('/usr/lib/systemd/system'),Path('/lib/systemd/system'))):
            raise ValueError('Installation refuses an existing service namespace')
        found=subprocess.run(['systemctl','show',name,'-p','LoadState','--value'],capture_output=True,text=True,check=True)
        if found.stdout.strip()!='not-found':
            raise ValueError('Installation refuses a loaded service namespace')
    subprocess.run(['systemd-analyze','verify',*[str((directory/n).resolve()) for n in manifest['unit_sha256']]],check=True)
    for name in manifest['unit_sha256']:
        # Exclusive creation preserves every earlier namespace, including symlinks.
        with (unit_dir/name).open('xb') as stream:stream.write((directory/name).read_bytes())
        (unit_dir/name).chmod(0o644)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    result={'units_installed':list(manifest['unit_sha256']),'services_activated':False,
            'installation_manifest_sha256':hashlib.sha256((directory/'installation.json').read_bytes()).hexdigest()}
    (directory/'installed.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('prepare','preflight','install'))
    parser.add_argument('directory',type=Path)
    parser.add_argument('--application',type=Path)
    parser.add_argument('--backends',type=Path)
    parser.add_argument('--manifest-sha256')
    args=parser.parse_args()
    try:
        if args.command=='prepare':
            if not args.application or not args.backends:parser.error('prepare needs both layouts')
            prepare(json.loads(args.application.read_text()),json.loads(args.backends.read_text()),args.directory)
            result={'manifest_sha256':hashlib.sha256((args.directory/'installation.json').read_bytes()).hexdigest(),
                    'units':7,'services_activated':False}
        else:
            if sys.platform!='linux' or os.geteuid()!=0:parser.error('Native Ubuntu root is required for effective-user checks')
            manifest=reviewed(args.directory,args.manifest_sha256)
            result=preflight(manifest)
            if result['preflight_passed'] and args.command=='install':result.update(install_new(args.directory,manifest))
        print(json.dumps(result,indent=2))
        if result.get('preflight_passed') is False:raise SystemExit(1)
    except (OSError,ValueError,KeyError,TypeError,subprocess.CalledProcessError) as exc:
        parser.exit(2,f'Native installation incomplete ({type(exc).__name__}); retained files are unchanged or partial, inspect before retry\n')


if __name__=='__main__':main()

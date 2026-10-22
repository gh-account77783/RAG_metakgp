"""Render isolated native PostgreSQL, Keycloak and Neo4j units; never install or activate them."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import sys

from tools.ubuntu_service_files import quote, service_lines, write_units


ROLES = ('postgresql', 'keycloak', 'neo4j')
COMMON = {'user', 'state_dir', 'memory_bytes'}
ROLE_FIELDS = {
    'postgresql': COMMON | {'binary', 'library_dir', 'data_dir'},
    'keycloak': COMMON | {'home', 'java_home', 'environment_file'},
    'neo4j': COMMON | {'home', 'java_home', 'config_dir'},
}


def validate(layout):
    if (not isinstance(layout, dict) or set(layout) != {'schema_version', 'name', 'cpu_quota_percent', *ROLES}
            or type(layout['schema_version']) is not int or layout['schema_version'] != 1):
        raise ValueError('Expected the complete schema_version 1 backend layout')
    if not isinstance(layout['name'], str) or not re.fullmatch(r'graphmind-[a-z0-9][a-z0-9-]{0,39}', layout['name']):
        raise ValueError('Choose a graphmind- service name with lowercase letters/digits/hyphens')
    if type(layout['cpu_quota_percent']) is not int or not 1 <= layout['cpu_quota_percent'] <= 10000:
        raise ValueError('cpu_quota_percent must be between 1 and 10000')
    users, states = set(), []
    for role in ROLES:
        spec = layout[role]
        if not isinstance(spec, dict) or set(spec) != ROLE_FIELDS[role]:
            raise ValueError('Expected complete fields for ' + role)
        user = spec['user']
        if (not isinstance(user, str) or user == 'root' or user in users
                or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', user)):
            raise ValueError('Each backend needs a distinct non-root service account')
        users.add(user)
        if type(spec['memory_bytes']) is not int or spec['memory_bytes'] < 256*1024**2:
            raise ValueError('Each backend needs an explicit memory cap of at least 256 MiB')
        for field in ROLE_FIELDS[role] - {'user', 'memory_bytes'}:
            value = spec[field]; quote(value)
            p = PurePosixPath(value)
            if (not p.is_absolute() or '..' in p.parts or str(p) == '/'
                    or p.is_relative_to('/home') or p.is_relative_to('/root')):
                raise ValueError(field + ' must be an absolute non-root path outside protected homes')
        state = PurePosixPath(spec['state_dir'])
        if not state.is_relative_to('/var/lib') or len(state.parts) < 4:
            raise ValueError('Each state directory must be a dedicated directory under /var/lib')
        if any(state.is_relative_to(other) or other.is_relative_to(state) for other in states):
            raise ValueError('Backend state directories must not overlap')
        states.append(state)
        if role == 'postgresql' and not PurePosixPath(spec['data_dir']).is_relative_to(state):
            raise ValueError('PostgreSQL data_dir must be inside its state directory')


def render(layout):
    validate(layout)
    name = layout['name']
    units = {}
    for role in ROLES:
        spec = layout[role]
        environment = {'HOME': spec['state_dir']}
        after, requires, extra = ['network-online.target'], [], {}
        if role == 'postgresql':
            command = [spec['binary'], '-D', spec['data_dir']]
            environment['LD_LIBRARY_PATH'] = spec['library_dir']
            extra['kill_signal'] = 'SIGINT'
        else:
            environment['JAVA_HOME'] = spec['java_home']
            home = PurePosixPath(spec['home'])
            if role == 'keycloak':
                command = [str(home/'bin/kc.sh'), 'start', '--optimized']
                after.append(name + '-postgresql.service')
                requires.append(name + '-postgresql.service')
                extra.update(environment_files=(spec['environment_file'],), working_directory=spec['state_dir'])
            else:
                command = [str(home/'bin/neo4j'), 'console']
                environment.update(NEO4J_HOME=spec['home'], NEO4J_CONF=spec['config_dir'])
                extra['limit_nofile'] = 60000
        lines = service_lines(description='GraphMind native ' + role, user=spec['user'],
            command=command, writable=[spec['state_dir']], environment=environment, after=after,
            requires=requires, memory_bytes=spec['memory_bytes'], cpu_quota_percent=layout['cpu_quota_percent'],
            timeout_stop=120, **extra)
        units[name + '-' + role + '.service'] = '\n'.join(lines)
    return units


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('layout', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--verify', action='store_true', help='Verify on native Linux; accounts/binaries must already exist')
    args = parser.parse_args()
    if args.verify and sys.platform != 'linux':
        parser.error('--verify requires native Linux/systemd')
    layout = json.loads(args.layout.read_text(encoding='utf-8'))
    units = render(layout)
    if args.verify:
        import pwd
        identifiers = [pwd.getpwnam(layout[role]['user']).pw_uid for role in ROLES]
        if 0 in identifiers or len(set(identifiers)) != len(ROLES):
            parser.error('Backends must resolve to distinct non-root UIDs')
    write_units(units, args.output, verify=args.verify)
    print(json.dumps({'output': str(args.output), 'units': list(units), 'activated': False}))


if __name__ == '__main__':
    main()

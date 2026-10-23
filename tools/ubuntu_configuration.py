"""Prepare private configuration for a new native, loopback HTTPS installation.

This command never initializes a database, creates an OS account or starts a
service. Model selections come from the operator's existing GraphMind TOML.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sys
import tomllib

from tools.ubuntu_installation import complete_layout


ROOT = Path(__file__).resolve().parents[1]
PORTS = ('postgresql', 'keycloak_http', 'keycloak_https', 'keycloak_management',
         'neo4j_bolt', 'neo4j_http')
EXTRA = {'schema_version', 'application', 'backends', 'ports', 'import_dir',
         'identity_ca_file', 'keycloak_certfile', 'keycloak_keyfile'}


@dataclass(frozen=True)
class PrivateFile:
    target: str
    role: str
    mode: int
    content: bytes = field(repr=False)


def path(value):
    if not isinstance(value, str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', value):
        raise ValueError('Use absolute native paths without whitespace or shell metacharacters')
    result = PurePosixPath(value)
    if '..' in result.parts or str(result) == '/' or result.is_relative_to('/home') or result.is_relative_to('/root'):
        raise ValueError('Native configuration inputs must be outside protected homes')
    return result


def validate(profile):
    if set(profile) != EXTRA or profile['schema_version'] != 1:
        raise ValueError('Expected the complete schema_version 1 configuration profile')
    app, backends = profile['application'], profile['backends']
    complete_layout(app, backends)
    if not {'ssl_certfile', 'ssl_keyfile'} <= set(app):
        raise ValueError('This bootstrap profile requires a validating HTTPS reader')
    if set(profile['ports']) != set(PORTS):
        raise ValueError('All six backend ports must be explicit')
    ports = [*profile['ports'].values(), *(app[k] for k in ('reader_port', 'chroma_port', 'ollama_port'))]
    if any(type(p) is not int or not 1024 <= p <= 65535 for p in ports) or len(set(ports)) != 9:
        raise ValueError('Choose nine distinct unprivileged loopback ports')
    root = path(app['config']).parent
    if root.parent != PurePosixPath('/etc'):
        raise ValueError('Use a dedicated configuration directory directly under /etc')
    expected = {'config': root/'reader.toml', 'dotenv': root/'reader.env'}
    if any(path(app[k]) != value for k, value in expected.items()):
        raise ValueError('Use reader.toml and reader.env in one dedicated configuration directory')
    if path(backends['keycloak']['environment_file']) != root/'keycloak.env' or path(backends['neo4j']['config_dir']) != root/'neo4j':
        raise ValueError('Backend configuration must share the dedicated configuration root')
    inputs = [path(profile[k]) for k in ('identity_ca_file', 'keycloak_certfile', 'keycloak_keyfile')]
    inputs += [path(app[k]) for k in ('ssl_certfile', 'ssl_keyfile')]
    if any(p.is_relative_to(root) for p in inputs):
        raise ValueError('Retained TLS inputs must be outside the new configuration directory')
    imported = path(profile['import_dir'])
    protected = [path(app[k]) for k in ('data_dir', 'model_dir', 'ollama_home')]
    protected += [path(backends[r]['state_dir']) for r in ('postgresql', 'keycloak', 'neo4j')]
    protected += [root]
    if any(imported.is_relative_to(p) or p.is_relative_to(imported) for p in protected):
        raise ValueError('Import staging must be separate from state, models and configuration')
    return root


def environment(values):
    # Restricted paths and URL-safe generated secrets work identically in
    # GraphMind's dotenv parser and systemd EnvironmentFile; no shell is involved.
    if any('\n' in v or '\r' in v or '"' in v or '\\' in v for v in values.values()):
        raise ValueError('Environment values cannot contain escapes or newlines')
    return ''.join(k+'="'+v+'"\n' for k, v in values.items()).encode()


def render(profile, settings_toml: bytes):
    root = validate(profile)
    parsed = tomllib.loads(settings_toml.decode('utf-8-sig'))
    settings = parsed.get('graphmind', parsed)
    for key, value in settings.items():
        if value and any(word in key.lower() for word in ('secret', 'password', 'api_key', 'signing_key')):
            raise ValueError('The input TOML must contain no credentials')
    for key in ('reranker_dir', 'visual_model_dir'):
        if settings.get(key): path(settings[key])
    if settings.get('answer_mode', 'local') != 'local' or settings.get('embedding_provider', 'ollama') != 'ollama':
        raise ValueError('This bootstrap profile uses explicit local Ollama inference')
    app, backend, ports = profile['application'], profile['backends'], profile['ports']
    public = 'https://127.0.0.1:'+str(app['reader_port'])
    identity = 'https://127.0.0.1:'+str(ports['keycloak_https'])
    values = {
        'GRAPHMIND_PUBLIC_BASE_URL': public,
        'GRAPHMIND_IDENTITY_ISSUER_URL': identity+'/realms/graphmind',
        'GRAPHMIND_IDENTITY_CA_FILE': profile['identity_ca_file'],
        'GRAPHMIND_IDENTITY_MCP_AUDIENCE': public+'/mcp',
        'GRAPHMIND_IDENTITY_BROWSER_CLIENT_SECRET': secrets.token_urlsafe(36),
        'GRAPHMIND_IDENTITY_RESOURCE_CLIENT_SECRET': secrets.token_urlsafe(36),
        'GRAPHMIND_IDENTITY_ADMIN_CLIENT_SECRET': secrets.token_urlsafe(36),
        'GRAPHMIND_IDENTITY_FLOW_SIGNING_KEY': secrets.token_urlsafe(48),
        'GRAPHMIND_DATA_DIR': app['data_dir'],
        'GRAPHMIND_ALLOWED_IMPORT_ROOTS': profile['import_dir'],
        'GRAPHMIND_ANSWER_MODE': 'local', 'GRAPHMIND_EMBEDDING_PROVIDER': 'ollama',
        'OLLAMA_BASE_URL': 'http://127.0.0.1:'+str(app['ollama_port']),
        'GRAPHMIND_EMBEDDING_BASE_URL': 'http://127.0.0.1:'+str(app['ollama_port']),
        'GRAPHMIND_CHROMA_MODE': 'http', 'GRAPHMIND_CHROMA_HOST': '127.0.0.1',
        'GRAPHMIND_CHROMA_PORT': str(app['chroma_port']),
        'NEO4J_URI': 'bolt://127.0.0.1:'+str(ports['neo4j_bolt']),
        'NEO4J_USERNAME': 'neo4j', 'NEO4J_PASSWORD': secrets.token_urlsafe(36),
        'GRAPHMIND_BROWSER_COOKIE_SECURE': 'true',
    }
    substitutions = {**values, 'GRAPHMIND_GOOGLE_CLIENT_ID': '', 'GRAPHMIND_GOOGLE_CLIENT_SECRET': ''}
    template = (ROOT/'deploy/keycloak/graphmind-realm.template.json').read_text(encoding='utf-8')
    realm = json.loads(re.sub(r'\$\{([A-Z0-9_]+)\}', lambda m: substitutions[m[1]], template))
    # Only a service identity is provisioned. Human reader accounts use the
    # existing registration/host-administration flow and require no new authority.
    realm['users'] = [{'username': 'service-account-graphmind-admin', 'enabled': True,
        'serviceAccountClientId': 'graphmind-admin',
        'clientRoles': {'realm-management': ['query-users', 'view-users', 'manage-users']}}]
    kc = {
        'JAVA_OPTS_KC_HEAP': '-Xms128m -Xmx768m', 'KC_DB': 'postgres',
        'KC_DB_URL': 'jdbc:postgresql://127.0.0.1:'+str(ports['postgresql'])+'/graphmind_keycloak',
        'KC_DB_USERNAME': 'graphmind_keycloak', 'KC_DB_PASSWORD': secrets.token_urlsafe(36),
        'KC_DB_POOL_INITIAL_SIZE': '2', 'KC_DB_POOL_MIN_SIZE': '2', 'KC_DB_POOL_MAX_SIZE': '10',
        'KC_HTTP_ENABLED': 'true', 'KC_HTTP_HOST': '127.0.0.1', 'KC_HTTP_PORT': str(ports['keycloak_http']),
        'KC_HTTPS_PORT': str(ports['keycloak_https']),
        'KC_HTTPS_CERTIFICATE_FILE': profile['keycloak_certfile'], 'KC_HTTPS_CERTIFICATE_KEY_FILE': profile['keycloak_keyfile'],
        'KC_HTTP_MANAGEMENT_HOST': '127.0.0.1', 'KC_HTTP_MANAGEMENT_PORT': str(ports['keycloak_management']),
        'KC_HTTP_MANAGEMENT_SCHEME': 'http', 'KC_HOSTNAME': identity, 'KC_HOSTNAME_STRICT': 'true',
        'KC_CACHE': 'local', 'KC_BOOTSTRAP_ADMIN_USERNAME': app['name']+'-bootstrap',
        'KC_BOOTSTRAP_ADMIN_PASSWORD': secrets.token_urlsafe(36),
    }
    state = PurePosixPath(backend['neo4j']['state_dir'])
    neo = {**{'server.directories.'+k: str(state/v) for k, v in
               {'data':'data', 'logs':'logs', 'run':'run', 'import':'import', 'transaction.logs.root':'data/transactions'}.items()},
        'server.default_listen_address':'127.0.0.1', 'server.default_advertised_address':'127.0.0.1',
        'server.bolt.enabled':'true', 'server.bolt.listen_address':'127.0.0.1:'+str(ports['neo4j_bolt']),
        'server.bolt.advertised_address':'127.0.0.1:'+str(ports['neo4j_bolt']),
        'server.http.enabled':'true', 'server.http.listen_address':'127.0.0.1:'+str(ports['neo4j_http']),
        'server.https.enabled':'false', 'dbms.security.auth_enabled':'true', 'dbms.usage_report.enabled':'false',
        'server.memory.heap.initial_size':'256m', 'server.memory.heap.max_size':'512m',
        'server.memory.pagecache.size':'256m', 'dbms.memory.transaction.total.max':'128m', 'db.query.default_language':'CYPHER_25'}
    pg = backend['postgresql']
    postgresql = "\nlisten_addresses = '127.0.0.1'\nport = "+str(ports['postgresql'])+"\n"
    postgresql += "unix_socket_directories = '"+pg['state_dir']+"/socket'\npassword_encryption = 'scram-sha-256'\nshared_buffers = '128MB'\nmax_connections = 30\nwork_mem = '4MB'\njit = off\n"
    reader = {k:v for k,v in values.items() if k != 'GRAPHMIND_IDENTITY_ADMIN_CLIENT_SECRET'}
    return [PrivateFile(str(root/'reader.toml'), 'application', 0o640, settings_toml),
        PrivateFile(str(root/'reader.env'), 'application', 0o640, environment(reader)),
        PrivateFile(str(root/'host-admin.env'), 'root', 0o600, environment(values)),
        PrivateFile(str(root/'keycloak.env'), 'root', 0o600, environment(kc)),
        PrivateFile(str(root/'realm-import.json'), 'root', 0o600, (json.dumps(realm,indent=2)+'\n').encode()),
        PrivateFile(str(root/'neo4j/neo4j.conf'), 'neo4j', 0o640, ''.join(k+'='+v+'\n' for k,v in neo.items()).encode()),
        PrivateFile(pg['data_dir']+'/graphmind.conf', 'postgresql', 0o600, postgresql.encode())]


def prepare(profile, settings_toml, output):
    files = render(profile, settings_toml)
    output.mkdir(mode=0o700)  # Exclusive: a retry cannot replace credentials.
    os.chmod(output, 0o700)
    manifest = {'schema_version':1, 'name':profile['application']['name'], 'files':[],
        'profile_sha256':hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()}
    for index, item in enumerate(files):
        name = str(index)+'.private'
        fd = os.open(output/name, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd,'wb') as stream: stream.write(item.content)
        manifest['files'].append({'file':name, 'target':item.target, 'role':item.role,
                                 'mode':item.mode, 'sha256':hashlib.sha256(item.content).hexdigest()})
    (output/'configuration.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    return manifest


def install(profile, directory, approved):
    """Install a reviewed fresh /etc tree; PostgreSQL includes follow initdb."""
    import pwd
    root=Path(str(validate(profile)))
    manifest_path=directory/'configuration.json'
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest()!=approved:
        raise ValueError('Configuration manifest changed')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest['profile_sha256']!=hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest():
        raise ValueError('Configuration profile changed')
    if os.path.lexists(root):raise ValueError('Existing configuration is preserved; use update/recovery')
    expected=render(profile,b'')
    rows=manifest['files']
    if len(rows)!=len(expected):raise ValueError('Expected seven private files')
    accounts={'application':profile['application']['user'],'root':'root',
              **{r:profile['backends'][r]['user'] for r in ('postgresql','keycloak','neo4j')}}
    identifiers={r:pwd.getpwnam(user) for r,user in accounts.items()}
    if len({a.pw_uid for a in identifiers.values()})!=5 or any(a.pw_uid==0 for r,a in identifiers.items() if r!='root'):
        raise ValueError('Four distinct non-root accounts are required')
    contents=[]
    for index,(row,item) in enumerate(zip(rows,expected)):
        if (row['file']!=str(index)+'.private' or (row['target'],row['role'],row['mode'])!=(item.target,item.role,item.mode)):
            raise ValueError('Configuration targets changed')
        source=directory/row['file']
        if source.is_symlink():raise ValueError('Staging files must not be symlinks')
        content=source.read_bytes()
        if hashlib.sha256(content).hexdigest()!=row['sha256']:raise ValueError('Configuration bytes changed')
        contents.append(content)
    root.mkdir(mode=0o755);root.chmod(0o755)
    neo=root/'neo4j';neo.mkdir(mode=0o750);os.chown(neo,0,identifiers['neo4j'].pw_gid);neo.chmod(0o750)
    installed=[]
    for row,content in zip(rows,contents):
        if row['role']=='postgresql':continue  # initdb must see an empty data directory.
        target=Path(row['target'])
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,row['mode'])
        with os.fdopen(fd,'wb') as stream:stream.write(content)
        os.chown(target,0,identifiers[row['role']].pw_gid);target.chmod(row['mode'])
        installed.append(str(target))
    return {'configuration_installed':installed,'postgresql_include_still_staged':True,'services_activated':False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    creation=commands.add_parser('prepare')
    creation.add_argument('profile',type=Path)
    creation.add_argument('settings',type=Path,help='Secret-free TOML with explicitly qualified model choices')
    creation.add_argument('directory',type=Path,help='New protected staging directory')
    deployment=commands.add_parser('install')
    deployment.add_argument('profile',type=Path)
    deployment.add_argument('directory',type=Path)
    deployment.add_argument('--manifest-sha256',required=True)
    args = parser.parse_args()
    try:
        profile=json.loads(args.profile.read_text(encoding='utf-8'))
        if args.command=='prepare':
            prepare(profile,args.settings.read_bytes(),args.directory)
            digest=hashlib.sha256((args.directory/'configuration.json').read_bytes()).hexdigest()
            result={'configuration_manifest_sha256':digest,'files':7,'services_activated':False}
        else:
            if sys.platform!='linux' or os.geteuid()!=0:parser.error('Native Ubuntu root is required')
            result=install(profile,args.directory,args.manifest_sha256)
        print(json.dumps(result))
    except (OSError,ValueError,KeyError,TypeError):
        parser.exit(2,'Configuration incomplete; protected staging is retained for inspection\n')


if __name__ == '__main__': main()

"""Prepare exclusive private inputs for a new Linux Docker Compose installation.

This command does not contact Docker, initialize stores, download models or start
services. Use CPython 3.13 and an externally approved Linux offline bundle.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tomllib

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.identity_configuration import render_realm

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {'schema_version', 'project_name', 'reader_host', 'identity_host', 'acme_email',
          'bundle_dir', 'bundle_manifest_sha256', 'models_dir', 'imports_dir', 'answer_model',
          'visual_answer_model', 'retrieval_profile', 'smtp_file', 'google_file', 'secret_gid'}
BUILD_INPUTS = (
    'tools/bundle_installer.py', 'tools/build_keycloak_recovery.py',
    'deploy/keycloak/recovery-src/org/graphmind/keycloak/ExistingPasswordResetFactory.java',
    'deploy/keycloak/recovery-src/org/graphmind/keycloak/PasswordRevocationFactory.java',
    'deploy/keycloak/recovery-src/META-INF/services/org.keycloak.authentication.AuthenticatorFactory',
    'deploy/keycloak/recovery-src/META-INF/services/org.keycloak.events.EventListenerProviderFactory',
    'deploy/keycloak/themes/graphmind/email/theme.properties',
    'deploy/keycloak/themes/graphmind/email/messages/messages_en.properties',
)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def directory(value):
    if not isinstance(value, str) or not value or any(c in value for c in '\r\n\0$#\"\''):
        raise ValueError('Directory inputs cannot contain controls, quotes or interpolation')
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError('An existing directory is required')
    return path


def hostname(value):
    if not isinstance(value, str) or len(value) > 253 or '.' not in value:
        raise ValueError('Supply two distinct public DNS hostnames')
    labels = value.split('.')
    if any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels):
        raise ValueError('Use lowercase DNS hostnames without URL/path/port syntax')
    if all(label.isdigit() for label in labels) or value.endswith('.localhost'):
        raise ValueError('This deployment uses public DNS and validating HTTPS')
    return value


def dotenv(values):
    return ''.join(key + '=' + json.dumps(str(value), ensure_ascii=True) + '\n'
                   for key, value in values.items()).encode()


def smtp_settings(path):
    values = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(values, dict) or not {'host', 'port', 'from', 'starttls', 'ssl', 'auth'} <= values.keys():
        raise ValueError('SMTP JSON needs host/port/from/starttls/ssl/auth')
    if not all(isinstance(v, str) and '\n' not in v and '\r' not in v for v in values.values()):
        raise ValueError('SMTP settings must be single-line strings')
    if values['starttls'] not in {'true', 'false'} or values['ssl'] not in {'true', 'false'}:
        raise ValueError('SMTP TLS flags must be true or false')
    if (values['starttls'] == 'true') == (values['ssl'] == 'true'):
        raise ValueError('Choose STARTTLS or implicit TLS for the public signup profile')
    if not values['port'].isdigit() or not 1 <= int(values['port']) <= 65535:
        raise ValueError('Invalid SMTP port')
    if not values['host'] or '@' not in values['from'] or values['auth'] not in {'true', 'false'}:
        raise ValueError('Invalid SMTP host/from/auth')
    if values['auth'] == 'true' and (not values.get('user') or not values.get('password')
                                   or values['password'] == 'REPLACE_IN_A_PRIVATE_COPY'):
        raise ValueError('Authenticated SMTP needs private credentials')
    return values


def render(profile, output):
    if set(profile) != FIELDS or profile['schema_version'] != 1:
        raise ValueError('Expected the complete schema_version 1 Docker profile')
    if not re.fullmatch(r'graphmind(?:-[a-z0-9][a-z0-9-]{0,39})?', profile['project_name']):
        raise ValueError('Use a new graphmind or graphmind-... Compose project')
    reader, identity = hostname(profile['reader_host']), hostname(profile['identity_host'])
    if reader == identity:
        raise ValueError('Reader and identity hostnames must differ')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', profile['acme_email']):
        raise ValueError('An ACME contact email is required')
    gid = profile['secret_gid']
    if type(gid) is not int or not 1 <= gid < 2**31:
        raise ValueError('secret_gid must be a non-root supplementary group ID')
    bundle, models, imports = (directory(profile[key]) for key in ('bundle_dir', 'models_dir', 'imports_dir'))
    output = Path(output).resolve()
    if any(c in output.as_posix() for c in '\r\n\0$#\"\''):
        raise ValueError('Private output path cannot contain interpolation or controls')
    paths = [bundle, models, imports, output]
    if any(a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(paths) for b in paths[i+1:]):
        raise ValueError('Bundle/models/imports/private configuration must be separate directories')
    approved = profile['bundle_manifest_sha256']
    if not isinstance(approved, str) or not re.fullmatch(r'[a-f0-9]{64}', approved):
        raise ValueError('Supply an externally approved bundle manifest SHA256')
    manifest_path = bundle / 'manifest.json'
    if digest(manifest_path) != approved:
        raise ValueError('Bundle manifest does not match the approved hash')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    if manifest.get('target') != {'implementation': 'CPython', 'python': '3.13', 'platform': 'linux-x86_64'}:
        raise ValueError('The Docker image needs a Linux x86-64 CPython 3.13 bundle')
    extras = set(manifest.get('extras', []))
    if not {'tree', 'visual'} <= extras:
        raise ValueError('The three-engine image requires tree and visual bundle extras')
    for key in ('answer_model', 'visual_answer_model'):
        if not isinstance(profile[key], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:@+-]*', profile[key]):
            raise ValueError('Supply explicit retained local model identifiers')
    if profile['retrieval_profile'] not in {'legacy', 'hybrid'}:
        raise ValueError('retrieval_profile must be legacy or hybrid')
    required = ['ollama', 'visual'] + (['reranker'] if profile['retrieval_profile'] == 'hybrid' else [])
    if any(not (models / part).is_dir() for part in required):
        raise ValueError('Retained model subdirectories are missing')
    if profile['retrieval_profile'] == 'hybrid' and 'rerank' not in extras:
        raise ValueError('Hybrid requires the rerank bundle extra')
    values = {
        'GRAPHMIND_PUBLIC_BASE_URL': 'https://' + reader,
        'GRAPHMIND_IDENTITY_ISSUER_URL': 'https://' + identity + '/realms/graphmind',
        'GRAPHMIND_IDENTITY_MCP_AUDIENCE': 'https://' + reader + '/mcp',
        'GRAPHMIND_IDENTITY_BROWSER_CLIENT_SECRET': secrets.token_urlsafe(36),
        'GRAPHMIND_IDENTITY_RESOURCE_CLIENT_SECRET': secrets.token_urlsafe(36),
        'GRAPHMIND_IDENTITY_ADMIN_CLIENT_SECRET': secrets.token_urlsafe(36),
        'GRAPHMIND_IDENTITY_FLOW_SIGNING_KEY': secrets.token_urlsafe(48),
        'NEO4J_PASSWORD': secrets.token_urlsafe(36),
    }
    substitutions = {**values, 'GRAPHMIND_GOOGLE_CLIENT_ID': '', 'GRAPHMIND_GOOGLE_CLIENT_SECRET': ''}
    google = None
    if profile['google_file']:
        google = json.loads(Path(profile['google_file']).read_text(encoding='utf-8-sig'))
        if set(google) != {'clientId', 'clientSecret'} or not all(isinstance(v, str) and v for v in google.values()):
            raise ValueError('Google JSON needs clientId and clientSecret')
        substitutions.update(GRAPHMIND_GOOGLE_CLIENT_ID=google['clientId'],
                             GRAPHMIND_GOOGLE_CLIENT_SECRET=google['clientSecret'])
    realm = render_realm((ROOT / 'deploy/keycloak/graphmind-realm.template.json').read_text(encoding='utf-8'), substitutions)
    realm['smtpServer'] = smtp_settings(profile['smtp_file'])
    if google:
        next(provider for provider in realm['identityProviders'] if provider['alias'] == 'google')['enabled'] = True
    settings = tomllib.loads((ROOT / 'config/graphmind.example.toml').read_text(encoding='utf-8-sig'))['graphmind']
    settings.update(data_dir='/var/lib/graphmind', allowed_import_roots=['/imports'],
        enabled_engines=['graphmind', 'tree_navigation', 'multimodal'],
        chroma_mode='http', chroma_host='127.0.0.1', chroma_port=18001,
        ollama_base_url='http://127.0.0.1:11434', embedding_base_url='http://127.0.0.1:11434',
        answer_mode='local', answer_model=profile['answer_model'],
        visual_answer_model=profile['visual_answer_model'], visual_model_dir='/models/visual',
        retrieval_profile=profile['retrieval_profile'],
        reranker_dir='/models/reranker' if profile['retrieval_profile'] == 'hybrid' else '',
        neo4j_uri='bolt://127.0.0.1:7687', public_base_url=values['GRAPHMIND_PUBLIC_BASE_URL'],
        identity_issuer_url=values['GRAPHMIND_IDENTITY_ISSUER_URL'],
        identity_mcp_audience=values['GRAPHMIND_IDENTITY_MCP_AUDIENCE'],
        browser_cookie_secure=True, answer_max_retries=0, max_concurrent_queries=1)
    toml = '[graphmind]\n' + ''.join(key + ' = ' + json.dumps(value) + '\n' for key, value in settings.items())
    reader_env = {key: value for key, value in values.items() if key != 'GRAPHMIND_IDENTITY_ADMIN_CLIENT_SECRET'}
    admin_env = {'GRAPHMIND_IDENTITY_ADMIN_CLIENT_SECRET': values['GRAPHMIND_IDENTITY_ADMIN_CLIENT_SECRET'],
                 'NEO4J_PASSWORD': values['NEO4J_PASSWORD']}
    compose = {
        'COMPOSE_PROJECT_NAME': profile['project_name'], 'GRAPHMIND_CONFIG_DIR': output.as_posix(),
        'GRAPHMIND_SOURCE_CONTEXT': (output / 'build-source').as_posix(),
        'GRAPHMIND_BUNDLE_DIR': bundle.as_posix(), 'GRAPHMIND_BUNDLE_SHA256': approved,
        'GRAPHMIND_MODELS_DIR': models.as_posix(), 'GRAPHMIND_IMPORTS_DIR': imports.as_posix(),
        'GRAPHMIND_READER_HOST': reader, 'GRAPHMIND_IDENTITY_HOST': identity,
        'GRAPHMIND_ACME_EMAIL': profile['acme_email'], 'GRAPHMIND_SECRET_GID': str(gid),
    }
    return {
        'reader.toml': toml.encode(), 'reader.env': dotenv(reader_env),
        'host-admin.env': dotenv(admin_env), 'neo4j.env': dotenv({'NEO4J_AUTH': 'neo4j/' + values['NEO4J_PASSWORD']}),
        'postgres-password': secrets.token_urlsafe(36).encode(),
        'keycloak-bootstrap-password': secrets.token_urlsafe(36).encode(),
        'realm-import.json': (json.dumps(realm, indent=2) + '\n').encode(),
        'compose.env': dotenv(compose),
    }


def prepare(profile, output):
    output = Path(output).resolve()
    if output.exists() or output.is_symlink():
        raise FileExistsError('Existing private configuration is preserved; choose a new directory')
    files = render(profile, output)
    output.mkdir(mode=0o700)  # Exclusive; retries cannot rotate retained credentials.
    os.chmod(output, 0o700)
    for name, content in files.items():
        target = output / name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        if hasattr(os, 'chown'):
            os.chown(target, -1, profile['secret_gid'])
        os.chmod(target, 0o640)
    for relative in BUILD_INPUTS:
        source = ROOT / relative
        if source.is_symlink():
            raise ValueError('Public build input cannot be a symlink')
        target = output / 'build-source' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes().replace(b'\r\n', b'\n'))
    report = {'schema_version': 1, 'project_name': profile['project_name'],
        'configuration_directory': str(output), 'bundle_manifest_sha256': profile['bundle_manifest_sha256'],
        'bundle_verification': 'Metadata checked here; full wheel/lock/notices/revocation check runs in image build',
        'public_build_inputs': {name: digest(output / 'build-source' / name) for name in BUILD_INPUTS},
        'files_written': sorted(files), 'runtime_acceptance': 'not run'}
    (output / 'preparation.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', type=Path)
    parser.add_argument('output', type=Path, help='NEW private directory; never overwrite an existing deployment')
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('Prepare real secrets on the Linux Docker host so directory modes/group ownership are enforced')
    try:
        profile = json.loads(args.profile.read_text(encoding='utf-8-sig'))
        result = prepare(profile, args.output)
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(2, 'Docker configuration failed; check the profile/paths/approved hash. Partial output is preserved.\n')
    print(json.dumps(result, indent=2))  # Paths and public-source hashes only; never credentials.


if __name__ == '__main__':
    main()

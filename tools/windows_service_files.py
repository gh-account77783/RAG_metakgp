"""Render reviewable native Windows service specifications without activating services."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re


ROLES = ('postgresql', 'keycloak', 'neo4j', 'ollama', 'chroma', 'reader', 'worker')
APP_FIELDS = ('python', 'chroma', 'ollama', 'config', 'dotenv', 'data_dir', 'model_dir', 'ollama_home')
TLS_FIELDS = ('ssl_certfile', 'ssl_keyfile')


def text(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Expected nonempty text without control characters')
    return value


def path(value):
    text(value)
    p = PureWindowsPath(value)
    if (not p.is_absolute() or not re.fullmatch('[A-Za-z]:', p.drive)
            or '..' in p.parts or len(p.parts) < 2 or value != value.strip()
            or any(c in value[2:] for c in ':*?"<>|')):
        raise ValueError('Expected an absolute local Windows path below a drive root')
    return p


def namespace(value):
    if not isinstance(value, str) or not re.fullmatch(r'graphmind-[a-z0-9][a-z0-9-]{0,39}', value):
        raise ValueError('Choose a graphmind- namespace using lowercase letters/digits/hyphens')
    return value


def command(values):
    if not isinstance(values, list) or not values:
        raise ValueError('Expected an executable argument array')
    for value in values:
        text(value)
    executable = path(values[0])
    if (executable.suffix.lower() != '.exe' or executable.name.lower() in
            {'cmd.exe', 'powershell.exe', 'pwsh.exe', 'wscript.exe', 'cscript.exe'}):
        raise ValueError('Commands require a native .exe; shell and batch commands are excluded')
    return values


def environment(values):
    if not isinstance(values, dict):
        raise ValueError('Expected an environment object')
    for key, value in values.items():
        if not re.fullmatch('[A-Z][A-Z0-9_]*', key):
            raise ValueError('Environment names must be uppercase identifiers')
        text(value)
    if len({key.upper() for key in values}) != len(values):
        raise ValueError('Duplicate environment names')
    return values


def validate_spec(spec):
    fields = {'schema_version', 'name', 'role', 'command', 'environment', 'environment_file',
              'cwd', 'state_dir', 'dependencies', 'stop_command', 'stop_timeout_seconds', 'memory_bytes'}
    if not isinstance(spec, dict) or set(spec) != fields or type(spec['schema_version']) is not int or spec['schema_version'] != 1:
        raise ValueError('Expected complete schema_version 1 service specification')
    role = spec['role']
    if role not in ROLES or not spec['name'].endswith('-' + role):
        raise ValueError('Invalid service role/name')
    base = namespace(spec['name'][:-len(role)-1])
    command(spec['command']); environment(spec['environment'])
    for field in ('cwd', 'state_dir'):
        path(spec[field])
    if spec['environment_file'] is not None:
        path(spec['environment_file'])
    if spec['stop_command'] is not None:
        command(spec['stop_command'])
    if (not isinstance(spec['dependencies'], list) or len(set(spec['dependencies'])) != len(spec['dependencies'])
            or any(dep not in {base + '-' + item for item in ROLES if item != role} for dep in spec['dependencies'])):
        raise ValueError('Dependencies must belong to the same installation')
    if type(spec['stop_timeout_seconds']) is not int or not 10 <= spec['stop_timeout_seconds'] <= 3600:
        raise ValueError('Choose an explicit bounded graceful stop timeout')
    if type(spec['memory_bytes']) is not int or spec['memory_bytes'] < 256 * 1024**2:
        raise ValueError('Each service needs an explicit job memory cap of at least 256 MiB')
    return spec


def render(layout):
    fields = {'schema_version', 'name', *APP_FIELDS, 'reader_port', 'chroma_port', 'ollama_port',
              'memory_bytes', 'backends'}
    if (not isinstance(layout, dict) or not fields <= set(layout) <= fields | set(TLS_FIELDS)
            or type(layout['schema_version']) is not int or layout['schema_version'] != 1
            or len(set(layout) & set(TLS_FIELDS)) not in (0, 2)):
        raise ValueError('Expected complete schema_version 1 Windows layout')
    name = namespace(layout['name'])
    for key in (*APP_FIELDS, *(key for key in TLS_FIELDS if key in layout)):
        path(layout[key])
    if path(layout['python']).parent != path(layout['chroma']).parent:
        raise ValueError('Python and Chroma must belong to one immutable release')
    ports = [layout[key] for key in ('reader_port', 'chroma_port', 'ollama_port')]
    if any(type(p) is not int or not 1024 <= p <= 65535 for p in ports) or len(set(ports)) != 3:
        raise ValueError('Choose three distinct unprivileged loopback ports')
    if not isinstance(layout['memory_bytes'], dict) or set(layout['memory_bytes']) != set(ROLES):
        raise ValueError('Specify all seven service memory caps')
    backend = layout['backends']
    if not isinstance(backend, dict) or set(backend) != set(ROLES[:3]):
        raise ValueError('Specify exactly PostgreSQL, Keycloak and Neo4j backends')
    states = [path(layout[key]) for key in ('data_dir', 'model_dir', 'ollama_home')]
    for role in ROLES[:3]:
        spec = backend[role]
        if set(spec) != {'command', 'environment', 'environment_file', 'cwd', 'state_dir', 'stop_command'}:
            raise ValueError('Expected complete backend specification')
        states.append(path(spec['state_dir']))
    if any(a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(states) for b in states[i+1:]):
        raise ValueError('Application, models, Ollama home and backend state must not overlap')
    release = path(layout['python']).parent.parent
    if any(release.is_relative_to(p) or p.is_relative_to(release) for p in states):
        raise ValueError('Release software and mutable state/model directories must not overlap')
    data = path(layout['data_dir'])
    endpoint = f"http://127.0.0.1:{layout['ollama_port']}"
    app_env = {
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
        'GRAPHMIND_DATA_DIR': str(data), 'GRAPHMIND_CHROMA_MODE': 'http',
        'GRAPHMIND_CHROMA_HOST': '127.0.0.1', 'GRAPHMIND_CHROMA_PORT': str(layout['chroma_port']),
        'GRAPHMIND_ANSWER_MODE': 'local', 'OLLAMA_BASE_URL': endpoint,
        'GRAPHMIND_EMBEDDING_BASE_URL': endpoint, 'HF_HUB_OFFLINE': '1',
        'TRANSFORMERS_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
        'HOME': str(data/'home'), 'USERPROFILE': str(data/'home'),
        'HF_HOME': str(data/'cache/huggingface'), 'TORCH_HOME': str(data/'cache/torch'),
        'MPLCONFIGDIR': str(data/'cache/matplotlib'),
    }
    app = [layout['python'], '-B', '-m', 'graphmind', '--config', layout['config'], '--dotenv', layout['dotenv']]
    tls = [value for key in TLS_FIELDS if key in layout for value in ('--' + key.replace('_', '-'), layout[key])]
    defaults = {
        'ollama': {'command': [layout['ollama'], 'serve'], 'environment': {
            'HOME': layout['ollama_home'], 'USERPROFILE': layout['ollama_home'],
            'OLLAMA_MODELS': layout['model_dir'], 'OLLAMA_HOST': f"127.0.0.1:{layout['ollama_port']}",
            'OLLAMA_KEEP_ALIVE': '0', 'OLLAMA_MAX_LOADED_MODELS': '1', 'OLLAMA_NUM_PARALLEL': '1',
            'OLLAMA_NO_CLOUD': '1', 'OLLAMA_NOHISTORY': '1'}, 'state_dir': layout['ollama_home']},
        'chroma': {'command': [layout['chroma'], 'run', '--path', str(data/'chroma'), '--host',
                               '127.0.0.1', '--port', str(layout['chroma_port'])],
                   'environment': {'ANONYMIZED_TELEMETRY': 'False'}, 'state_dir': str(data/'chroma')},
        'reader': {'command': app + ['serve', '--host', '127.0.0.1', '--port', str(layout['reader_port']), *tls],
                   'environment': app_env, 'state_dir': layout['data_dir']},
        'worker': {'command': app + ['worker', '--poll-seconds', '5'],
                   'environment': app_env, 'state_dir': layout['data_dir']},
    }
    result = {}
    for role in ROLES:
        spec = {'environment_file': None, 'stop_command': None, **(backend[role] if role in backend else defaults[role])}
        spec.setdefault('cwd', spec['state_dir'])
        spec.update(schema_version=1, name=name + '-' + role, role=role,
                    dependencies=[name + '-' + r for r in (('postgresql',) if role == 'keycloak' else
                                  ('keycloak', 'neo4j', 'chroma', 'ollama') if role in ('reader', 'worker') else ())],
                    stop_timeout_seconds=2000 if role in ('reader', 'worker') else 120,
                    memory_bytes=layout['memory_bytes'][role])
        result[spec['name']] = validate_spec(spec)
    return result


def prepare(layout, output):
    specs = render(layout)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name, spec in specs.items():
        target = output/(name + '.json')
        target.write_text(json.dumps(spec, indent=2) + '\n', encoding='utf-8')
        hashes[target.name] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {'schema_version': 1, 'name': layout['name'], 'service_sha256': hashes,
                'layout_sha256': hashlib.sha256(json.dumps(layout, sort_keys=True).encode()).hexdigest()}
    (output/'installation.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('layout', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    prepare(json.loads(args.layout.read_text(encoding='utf-8')), args.output)
    print(json.dumps({'manifest_sha256': hashlib.sha256((args.output/'installation.json').read_bytes()).hexdigest(),
                      'services_activated': False}))


if __name__ == '__main__':
    main()

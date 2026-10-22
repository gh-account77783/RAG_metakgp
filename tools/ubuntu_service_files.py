"""Render private Ubuntu systemd services; never install or activate them."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


PATH_FIELDS = ("python", "chroma", "ollama", "config", "dotenv", "data_dir", "model_dir", "ollama_home")
ROLES = ('reader', 'worker', 'chroma', 'ollama')
FIELDS = {"schema_version", "name", "user", *PATH_FIELDS, "reader_port", "chroma_port", "ollama_port",
          "cpu_quota_percent", *(role + '_memory_bytes' for role in ROLES)}


def quote(value):
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Unit values must be text without control characters")
    return json.dumps(value.replace('%', '%%'), ensure_ascii=False)


def validate(layout):
    if not isinstance(layout, dict) or set(layout) != FIELDS or layout["schema_version"] != 1:
        raise ValueError("Expected the complete schema_version 1 service layout")
    if not re.fullmatch(r"graphmind-[a-z0-9][a-z0-9-]{0,39}", str(layout["name"])):
        raise ValueError("Service name must start with graphmind- and use lowercase letters/digits/hyphens")
    user = layout["user"]
    if not isinstance(user, str) or user == 'root' or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", user):
        raise ValueError("Choose a non-root service account")
    for field in PATH_FIELDS:
        value = layout[field]; quote(value)
        path = PurePosixPath(value)
        if not path.is_absolute() or '..' in path.parts or str(path) == '/':
            raise ValueError(f"{field} must be an absolute non-root path")
        if path.is_relative_to('/home') or path.is_relative_to('/root'):
            raise ValueError(f"{field} must be outside protected home directories")
    ports = [layout[field] for field in ('reader_port', 'chroma_port', 'ollama_port')]
    if any(type(port) is not int or not 1024 <= port <= 65535 for port in ports) or len(set(ports)) != 3:
        raise ValueError("Choose three distinct unprivileged loopback ports")
    if type(layout['cpu_quota_percent']) is not int or not 1 <= layout['cpu_quota_percent'] <= 10000:
        raise ValueError("cpu_quota_percent must be between 1 and 10000")
    if any(type(layout[role + '_memory_bytes']) is not int or layout[role + '_memory_bytes'] < 256*1024**2
           for role in ROLES):
        raise ValueError("Each service needs an explicit memory cap of at least 256 MiB")
    data, home, models = [PurePosixPath(layout[field]) for field in ('data_dir', 'ollama_home', 'model_dir')]
    if any(first.is_relative_to(second) or second.is_relative_to(first)
           for first, second in ((data, home), (data, models), (home, models))):
        raise ValueError("Application data, model weights and Ollama home must be separate directories")


def render(layout):
    validate(layout)
    name, user = layout['name'], layout['user']
    data = PurePosixPath(layout['data_dir'])
    endpoint = f"http://127.0.0.1:{layout['ollama_port']}"
    app_environment = {
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
        'GRAPHMIND_DATA_DIR': str(data), 'GRAPHMIND_CHROMA_MODE': 'http',
        'GRAPHMIND_CHROMA_HOST': '127.0.0.1', 'GRAPHMIND_CHROMA_PORT': str(layout['chroma_port']),
        'GRAPHMIND_ANSWER_MODE': 'local', 'OLLAMA_BASE_URL': endpoint,
        'GRAPHMIND_EMBEDDING_BASE_URL': endpoint, 'HF_HUB_OFFLINE': '1',
        'TRANSFORMERS_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
        'HOME': str(data/'home'), 'XDG_CACHE_HOME': str(data/'cache'),
        'HF_HOME': str(data/'cache/huggingface'), 'TORCH_HOME': str(data/'cache/torch'),
        'MPLCONFIGDIR': str(data/'cache/matplotlib'),
    }
    app = [layout['python'], '-B', '-m', 'graphmind', '--config', layout['config'], '--dotenv', layout['dotenv']]
    specs = {
        'ollama': ([layout['ollama'], 'serve'], [layout['ollama_home']], {
            'HOME': layout['ollama_home'], 'OLLAMA_MODELS': layout['model_dir'],
            'OLLAMA_HOST': f"127.0.0.1:{layout['ollama_port']}", 'OLLAMA_KEEP_ALIVE': '0',
            'OLLAMA_MAX_LOADED_MODELS': '1', 'OLLAMA_NUM_PARALLEL': '1',
            'OLLAMA_NO_CLOUD': '1', 'OLLAMA_NOHISTORY': '1'}, []),
        'chroma': ([layout['chroma'], 'run', '--path', str(data/'chroma'), '--host', '127.0.0.1',
                    '--port', str(layout['chroma_port'])], [str(data/'chroma')],
                    {'ANONYMIZED_TELEMETRY': 'False'}, []),
        'reader': (app + ['serve', '--host', '127.0.0.1', '--port', str(layout['reader_port'])],
                   [str(data)], app_environment, ['chroma', 'ollama']),
        'worker': (app + ['worker', '--poll-seconds', '5'], [str(data)], app_environment, ['chroma', 'ollama']),
    }
    units = {}
    for role, (command, writable, environment, dependencies) in specs.items():
        after = ['network-online.target', *(f'{name}-{item}.service' for item in dependencies)]
        lines = ['[Unit]', f'Description=GraphMind {role}', 'After=' + ' '.join(after),
                 'Wants=network-online.target', 'StartLimitIntervalSec=60', 'StartLimitBurst=5',
                 '', '[Service]', 'Type=exec', f'User={user}', f'Group={user}', 'UMask=0077',
                 # ':' disables systemd's environment expansion in command arguments.
                 'ExecStart=:' + ' '.join(quote(item) for item in command),
                 f"MemoryMax={layout[role + '_memory_bytes']}", f"CPUQuota={layout['cpu_quota_percent']}%",
                 'Restart=on-failure', 'RestartSec=5', 'TimeoutStopSec=2000',
                 # Signal the parent first; its fenced job may still own a worker.
                 'KillMode=mixed', 'NoNewPrivileges=true', 'CapabilityBoundingSet=',
                 'PrivateTmp=true', 'PrivateDevices=true', 'ProtectSystem=strict', 'ProtectHome=true',
                 'ProtectKernelTunables=true', 'ProtectKernelModules=true', 'ProtectControlGroups=true',
                 'RestrictSUIDSGID=true', 'ReadWritePaths=' + ' '.join(quote(item) for item in writable)]
        lines += ['Environment=' + quote(key + '=' + value) for key, value in environment.items()]
        lines += ['', '[Install]', 'WantedBy=multi-user.target', '']
        units[f'{name}-{role}.service'] = '\n'.join(lines)
    return units


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('layout', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--verify', action='store_true', help='Run native systemd-analyze verify; executables must exist')
    args = parser.parse_args()
    if args.verify and sys.platform != 'linux':
        parser.error('--verify requires native Linux/systemd')
    layout = json.loads(args.layout.read_text(encoding='utf-8'))
    units = render(layout)
    if args.verify:
        import pwd
        if pwd.getpwnam(layout['user']).pw_uid == 0:
            parser.error('The chosen service account resolves to UID 0')
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    digests = {}
    for name, body in units.items():
        path = args.output/name; path.write_text(body, encoding='utf-8', newline='\n')
        digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (args.output/'service-files.json').write_text(json.dumps({'schema_version': 1, 'unit_sha256': digests}, indent=2))
    if args.verify:
        with (args.output/'verify.log').open('w') as stream:
            subprocess.run(['systemd-analyze', 'verify', *(str((args.output/name).resolve()) for name in units)],
                           stdout=stream, stderr=subprocess.STDOUT, check=True)
    print(json.dumps({'output': str(args.output), 'units': list(units), 'activated': False}))


if __name__ == '__main__':
    main()

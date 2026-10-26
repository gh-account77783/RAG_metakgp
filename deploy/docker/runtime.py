"""Thin container process launcher; application behavior stays in GraphMind CLI."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def command(arguments):
    if not arguments:
        raise ValueError('Choose reader, worker, chroma, identity-setup or admin')
    role, *extra = arguments
    python = '/opt/graphmind/bin/python'
    if role == 'chroma' and not extra:
        return ['/opt/graphmind/bin/chroma', 'run', '--path', '/var/lib/chroma',
                '--host', '127.0.0.1', '--port', '18001']
    if role == 'identity-setup' and not extra:
        return [python, '-I', '-B', '/opt/graphmind-container/bootstrap_identity.py']
    if role not in {'reader', 'worker', 'admin'} or (extra and role != 'admin'):
        raise ValueError('Invalid container role or arguments')
    dotenv = '/run/secrets/host_admin_env' if role == 'admin' else '/run/secrets/reader_env'
    base = [python, '-I', '-B', '-m', 'graphmind.cli', '--config', '/etc/graphmind/reader.toml',
            '--dotenv', dotenv]
    if role == 'reader':
        return [*base, 'serve', '--host', '127.0.0.1', '--port', '18000']
    if role == 'worker':
        return [*base, 'worker']
    return [*base, *(extra or ['wizard'])]


def main():
    try:
        arguments = command(sys.argv[1:])
        Path(os.environ.get('HOME', '/var/lib/graphmind/home')).mkdir(parents=True, exist_ok=True, mode=0o700)
        os.execv(arguments[0], arguments)
    except (OSError, ValueError) as exc:
        raise SystemExit('Container launch failed: ' + type(exc).__name__) from None


if __name__ == '__main__':
    main()

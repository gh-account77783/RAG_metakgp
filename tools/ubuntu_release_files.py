"""Prepare reviewable Ubuntu Python-release overrides; never change live services."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath

try:
    from tools.ubuntu_service_files import quote, render
except ModuleNotFoundError:
    from ubuntu_service_files import quote, render


ROLES = ('reader', 'worker', 'chroma')


def release_overrides(layout, release):
    """Keep configuration/state/native models fixed while switching one Python closure."""
    original = render(layout)  # Reuse the complete service layout validation.
    quote(release)
    target = PurePosixPath(release)
    previous = PurePosixPath(layout['python']).parent.parent
    if (not target.is_absolute() or str(target) != release or '..' in target.parts
            or target == PurePosixPath('/') or target == previous
            or target.is_relative_to('/home') or target.is_relative_to('/root')):
        raise ValueError('Choose a different canonical absolute release outside protected homes')
    if (PurePosixPath(layout['python']) != previous/'bin/python'
            or PurePosixPath(layout['chroma']) != previous/'bin/chroma'):
        raise ValueError('Python and Chroma must belong to the same existing immutable release')
    paths = [previous, *(PurePosixPath(layout[key]) for key in ('data_dir', 'model_dir', 'ollama_home'))]
    if any(target.is_relative_to(path) or path.is_relative_to(target) for path in paths):
        raise ValueError('Release must be separate from previous release, state and model paths')
    changed = {**layout, 'python': str(target/'bin/python'), 'chroma': str(target/'bin/chroma')}
    candidate = render(changed)
    files = {}
    for role in ROLES:
        unit = f"{layout['name']}-{role}.service"
        command, = [line for line in candidate[unit].splitlines() if line.startswith('ExecStart=')]
        files[unit+'.d/50-graphmind-release.conf'] = '[Service]\nExecStart=\n'+command+'\n'
    manifest = {
        'schema_version': 1, 'name': layout['name'], 'previous_release': str(previous),
        'candidate_release': str(target), 'roles': list(ROLES), 'activated': False,
        'base_unit_sha256': {name: hashlib.sha256(body.encode('utf-8')).hexdigest()
                             for name,body in original.items()},
        'file_sha256': {name: hashlib.sha256(body.encode('utf-8')).hexdigest()
                        for name,body in files.items()},
    }
    return files, manifest


def prepare(layout, release, output):
    files, manifest = release_overrides(layout, release)
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name,body in files.items():
        path = output/name
        path.parent.mkdir()
        path.write_text(body, encoding='utf-8', newline='\n')
    (output/'release-files.json').write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('layout', type=Path, help='Existing nonsecret application service layout')
    parser.add_argument('release', help='Already installed and qualified new immutable Python environment')
    parser.add_argument('output', type=Path, help='New directory for reviewable overrides and hashes')
    args = parser.parse_args()
    manifest = prepare(json.loads(args.layout.read_text(encoding='utf-8')), args.release, args.output)
    print(json.dumps({'output':str(args.output), **manifest}))


if __name__ == '__main__':
    main()

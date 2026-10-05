"""Build the pinned Keycloak provider with JDK 21 and a 26.7.3 lib/lib/main directory.

No Maven download, runtime secret, password store or system installation required.
Caller supplies a verified JDK and exact installed Keycloak distribution libraries.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'deploy/keycloak/recovery-src'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jdk-bin', type=Path, required=True)
    parser.add_argument('--classpath-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    java = args.jdk_bin / ('java.exe' if os.name == 'nt' else 'java')
    javac = args.jdk_bin / ('javac.exe' if os.name == 'nt' else 'javac')
    version = subprocess.run([java, '-version'], check=True, capture_output=True, text=True).stderr
    if not ('version "21.' in version or 'openjdk 21.' in version):
        parser.error('JDK 21 is required for the pinned Keycloak provider')
    jars = sorted(args.classpath_dir.glob('*.jar'))
    required = ('keycloak-core', 'keycloak-server-spi', 'keycloak-server-spi-private', 'keycloak-services')
    for name in required:
        if not any(jar.name == 'org.keycloak.' + name + '-26.7.3.jar' for jar in jars):
            parser.error('Classpath must include exact Keycloak 26.7.3 library: ' + name)
    args.output.mkdir(parents=True, exist_ok=True)
    artifact = args.output / 'graphmind-keycloak-recovery-26.7.3.jar'
    if artifact.exists():
        parser.error('Existing provider artifact preserved; use a fresh output directory')
    with tempfile.TemporaryDirectory(prefix='graphmind-kc-recovery-') as temporary:
        classes = Path(temporary) / 'classes'; classes.mkdir()
        subprocess.run([javac, '--release', '21', '-g:none', '-classpath', os.pathsep.join(map(str, jars)),
                        '-d', classes, *sorted(SOURCE.rglob('*.java'))], check=True)
        shutil.copytree(SOURCE / 'META-INF', classes / 'META-INF')
        # Fixed ZIP metadata/order: deterministic provider, no build path/secret.
        with zipfile.ZipFile(artifact, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            for item in sorted(classes.rglob('*')):
                if item.is_file():
                    info = zipfile.ZipInfo(item.relative_to(classes).as_posix(), (2026, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o644 << 16
                    archive.writestr(info, item.read_bytes())
    report = {'keycloak': '26.7.3', 'java_release': 21,
              'provider_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
              'library_sha256': {jar.name: hashlib.sha256(jar.read_bytes()).hexdigest() for jar in jars},
              'sources_sha256': {item.relative_to(ROOT).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
                                 for item in sorted(SOURCE.rglob('*')) if item.is_file()}}
    (args.output / 'provider-build.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'library_sha256'}, indent=2))


if __name__ == '__main__':
    main()

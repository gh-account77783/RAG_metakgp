# Native release bundles

P7 build tooling runs from a development checkout with CPython 3.13 and the
existing constrained build/test dependencies. It is separate from the reader
process. Installing GraphMind must not resolve or download code or weights at
startup or during a query.

Generate a bundle separately on each supported native OS/architecture/profile.
Windows 11 and Server 2025 may use compatible win-amd64 wheels, but still need
independent native acceptance. Resolving Linux dependency markers on a Windows
builder is not Linux qualification.

The bundle directory contains:

- `wheelhouse/`: one selected compatible wheel for each distribution, including
  GraphMind and the complete active dependency closure.
- `requirements.lock`: exact package versions and SHA-256 hashes generated from
  those wheels, rather than a second hand-maintained dependency declaration.
- `manifest.json`: target interpreter/platform, selected extras, package metadata,
  artifact hashes and retained-notice hashes. It is written after validation.
- `notices/`: original notice/license files retained from the chosen wheels.

After approved wheels have been collected into a fresh bundle's `wheelhouse/`,
freeze and check it on its native target:

```console
python tools/release_bundle.py freeze /private/candidate --extra test
python tools/release_bundle.py verify /private/candidate
```

The tool rejects an incomplete or incompatible closure, unavailable extras,
duplicate distributions, foreign wheels/targets, direct-URL dependencies, changed
wheel/lock/notice bytes and attempts to overwrite a completed bundle. Verification
uses the extras in the manifest. Preserve failed/incomplete candidates for
diagnosis; absence of a validated manifest means the bundle is incomplete.

The existing constraints remain resolver inputs. They are not the generated
complete hash-checked installation specification. A wheelhouse may contain a
reviewed locally built wheel only with its separate pinned-source/build-input
provenance; do not compile arbitrary upstream sources on an operator's host.

For a previously verified bundle, a fresh isolated environment installs its
Python closure without a package index:

```console
python -m pip install --no-index --only-binary=:all: --find-links /private/candidate/wheelhouse --require-hashes -r /private/candidate/requirements.lock
python -m pip check
```

Keep the bundle manifest's approved hash outside the bundle as well. Hashes
identify bytes; they do not authenticate an arbitrary manifest or establish
software safety, license compatibility, model rights or native-service acceptance.
The metadata/license inventory requires review. Model weights, frontend/native
binaries, service configuration and their notices need separately versioned
artifact records. The tool does not download models or call an inference provider.

These mechanics follow [pip repeatable installs](https://pip.pypa.io/en/stable/topics/repeatable-installs/)
and [hash-checked installs](https://pip.pypa.io/en/stable/topics/secure-installs/).
Combined engine compatibility, retained native bundles, clean installs and real
three-platform acceptance remain P7 gates until their recorded checks pass.

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

To resolve a new bundle directly from the built package and constraints, use a
new destination directory:

```console
python tools/release_bundle.py collect /private/candidate --project-wheel /private/graphmind_rag_mcp-0.1.0-py3-none-any.whl --extra test --constraint config/constraints/core-py313.txt --constraint config/constraints/linux-py313.txt
```

Collection permits wheels only and uses the official PyPI index in pip's isolated
mode. Repeat `--artifact /private/reviewed.whl` for separately qualified local
wheels such as CPU-only inference builds. The manifest records their hashes and
the constraint-file hashes. A failed resolver leaves `collection.log` and an
incomplete directory; use a different destination for the next attempt.

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
python tools/release_bundle.py install /private/candidate --environment /private/new-environment
```

Installation verifies the bundle before creating the environment, then runs pip
with `--no-index`, `--only-binary=:all:` and `--require-hashes`, followed by
`pip check`. It refuses an existing environment or a path inside the bundle.
`bundle-install.json` records success only after both commands pass; a failed
installation retains its environment and log for diagnosis. This installs the
Python closure; configure native services separately.

Keep the bundle manifest's approved hash outside the bundle as well. Hashes
identify bytes; they do not authenticate an arbitrary manifest or establish
software safety, license compatibility, model rights or native-service acceptance.
The metadata/license inventory requires review. Model weights, frontend/native
binaries, service configuration and their notices need separately versioned
artifact records. The tool does not download models or call an inference provider.

For public Hugging Face models with root-level safetensors/config/tokenizer
assets, retain a separate model directory using an exact upstream commit:

```console
python tools/model_artifacts.py collect /private/model --model namespace/model --revision FULL_40_CHARACTER_COMMIT
python tools/model_artifacts.py verify /private/model
```

This collector verifies upstream LFS SHA-256 or Git blob identities, records a
SHA-256 for every retained file, preserves root README/license/notice files and
refuses mutable revisions or an existing destination. Python code, pickle weights
and nested files are excluded. It uses public HTTPS without reading a model token
or credential cache. Failed transfers retain partial files without a completion
manifest. Keep the approved `.graphmind-model.json` hash separately. Declared
model-card terms still require software, backbone, model and data rights review.
The collector performs no inference and does not enable an engine. Runtime
adapters must load verified local paths with downloads and remote code disabled.

These mechanics follow [pip repeatable installs](https://pip.pypa.io/en/stable/topics/repeatable-installs/)
and [hash-checked installs](https://pip.pypa.io/en/stable/topics/secure-installs/).
Combined engine compatibility, retained native bundles, clean installs and real
three-platform acceptance remain P7 gates until their recorded checks pass.

# Installing a retained bundle on a clean Python host

`release_bundle.py` is the build-time resolver and metadata/closure verifier. New
bundles also retain `install_bundle.py`, a standalone standard-library bootstrap.
The two entrypoints reuse one installer implementation. Bootstrap never resolves
from an index, downloads a model, installs a service or modifies an old environment.

Use the separately qualified native Python artifact, including `venv`/`ensurepip`.
Build one bundle per supported native interpreter/platform/profile. Windows 11
and Server 2025 require their own acceptance even when wheel tags match. The
bootstrap needs no preinstalled `packaging`, GraphMind or build environment.

Publish/retain **both** approved SHA-256 values outside the bundle: the installer
script and `manifest.json`. Check the installer hash with a trusted system tool
before executing it, then pass the approved manifest hash explicitly. Reading a
hash from the same untrusted download does not authenticate it; the executing
script cannot protect against its own prior replacement.

```console
sha256sum /private/candidate/install_bundle.py
python -I -B /private/candidate/install_bundle.py /private/candidate --manifest-sha256 APPROVED_MANIFEST_SHA256 --verify-only
python -I -B /private/candidate/install_bundle.py /private/candidate --manifest-sha256 APPROVED_MANIFEST_SHA256 --environment /opt/graphmind/releases/new
```

On Windows, use `Get-FileHash -Algorithm SHA256 -LiteralPath ...` for the trusted
hash check and a new environment under the approved installation directory.

Verification checks the approved manifest, native target, exact wheel names,
sizes/hashes, hash lock, retained notice inventory and bundled installer. The
build tool has already validated dependency metadata, tags, markers and extras.
Pip additionally enforces wheel compatibility and the dependency closure using
`--no-index --only-binary=:all: --require-hashes`; `pip check` is mandatory. No
installation success receipt is written until both commands pass.

Existing environments and overlapping bundle/install paths are refused. Preserve
partial environments/logs on failure and use a new path for a corrected attempt.
Keep the approved runtime and bundle for recovery. Older schema1 bundles without
a bundled installer remain verifiable through the build tool or a separately
trusted bootstrap; do not rewrite their approved manifest in place.

Native databases, identity configuration, model artifacts and protected state
remain separate installation inputs. Follow `deploy/native/README.md` for the
reviewed service layouts/update procedure. Retained notices and passing hash
checks are evidence for review, not software/model/data rights approval.

When a chosen wheel omits an upstream notice, retain the reviewed original text
as a supplemental file in a **new** bundle. Supply `--supplemental-notices
/private/reviewed/notices.json` to `release_bundle.py collect` or `freeze`.
The JSON inventory has `schema_version: 1` and a `notices` list. Each entry needs
`distribution` (canonical package name), exact `version`, `wheel_sha256`, `file`
(relative to the inventory), `sha256` of that file, public HTTPS `source_url` and
pinned `revision`. For example:

```json
{"schema_version":1,"notices":[{"distribution":"example","version":"1.0","wheel_sha256":"APPROVED_WHEEL_SHA256","file":"LICENSE","sha256":"APPROVED_LICENSE_SHA256","source_url":"https://publisher.example/commit/LICENSE","revision":"PINNED_COMMIT"}]}
```

The builder checks the selected wheel identity and notice bytes, retains the
text under `notices/PACKAGE/supplemental/`, and records its provenance in the
approved manifest. Both build-time and standalone verification enforce the same
binding. Upstream wheel bytes and older bundles remain unchanged. This records
review inputs; it does not infer missing terms or approve redistribution.

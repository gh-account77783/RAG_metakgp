# Linux Docker Compose deployment

Linux Docker Compose is GraphMind's default deployment route. The native
[Windows Server](../../tools/WINDOWS_INSTALLATION.md) and
[Ubuntu](../../tools/UBUNTU_INSTALLATION.md) paths remain alternatives.
This is a **deployment candidate**: code/configuration checks do not establish
an image build, running stack, login, inference, restart or release acceptance.
Complete the bounded runtime smoke below before advertising tested support.

## Prerequisites and artifacts

- Linux x86-64, Docker Engine/BuildKit and Compose 2.24 or newer. GPU, ARM and
  Windows containers are outside this initial profile. Windows 11 may host a
  Linux Docker environment; Docker Desktop is not supported on Windows Server.
- Two distinct public DNS names pointing at the host, inbound TCP 80/443 and an
  ACME contact email. Replace `rag.example.com` and `auth.example.com` below.
  Those host ports must be free; an existing native HTTPS proxy cannot share
  these port mappings.
- An approved Linux CPython 3.13 **CPU** offline bundle with `tree` and `visual`
  extras, plus `rerank` for hybrid retrieval. Obtain its manifest SHA256 from a
  separately trusted release record. Do not use Windows wheels or revoked bundles.
- Retained local Ollama models and the verified ColQwen2 artifact directory.
  No weights are downloaded automatically. Public model files and selected
  imports must be readable by application UID 10001.
- Private SMTP settings for email verification/recovery; Google login is
  optional and needs a separately configured OAuth application.
- Sufficient disk for images, model weights, stores and update copies. A visual
  process previously approached 12 GiB RAM; start planning around a 32 GiB host
  for this full CPU profile and measure it. Caps are not reservations or proof
  of fit. Docker does not remove memory/storage requirements.

Use the existing [bundle guide](../../documentation/RELEASE_BUNDLES.md) to build
a new candidate wheel and freeze its Linux closure. The collector accepts
`--extra tree --extra visual`, optionally `--extra rerank`, all three constraint
files, and separately reviewed CPU Torch/Torchvision wheels via `--artifact`.
An Ubuntu wheelhouse remains a candidate until its native libraries also load
in the chosen image. Keep the model manifests/digests and existing
`tools/model_artifacts.py verify` result with the candidate.

```text
/srv/graphmind/bundle/       manifest.json, requirements.lock, notices/, wheelhouse/
/srv/graphmind/models/
  ollama/                  public Ollama blobs/ and manifests/ only
  visual/                  retained ColQwen2 artifact directory
  reranker/                retained reranker, only for hybrid
/srv/graphmind/imports/     operator-selected source documents
/srv/graphmind-private/    private SMTP/Google/profile inputs and generated runtime
```

Model storage is mounted read-only. Keep it separate from original stores,
Ollama private keys and credential files; the new model-server home is a named
volume. Choose a new Compose project and new data volumes.

## Prepare private configuration

Copy `profile.example.json` to a private location and replace its names, paths,
model identifiers and approved bundle manifest hash. Set `secret_gid` to the
operator's non-root group ID (`id -g`). Copy `smtp.example.json` privately and
supply actual settings there: choose STARTTLS or implicit TLS, not both.
An optional private Google JSON file has `clientId` and `clientSecret`; point
`google_file` to it and register this broker redirect in that application:

```text
https://auth.example.com/realms/graphmind/broker/google/endpoint
```

On the Linux Docker host, generate a **new** output directory whose parent exists:

```sh
python3.13 tools/docker_configuration.py /srv/graphmind-private/profile.json \
  /srv/graphmind-private/runtime
```

This standard-library command checks profile/manifest metadata, generates
independent random client/database/bootstrap secrets and reuses the canonical
realm template. An existing directory is refused, preserving credentials on a
retry. It starts no service and downloads nothing. Full wheel/lock/notice and
revoked-release verification happens through the existing installer in the build.
Its report contains paths and public-source hashes, never credentials.

The private directory is 0700 and files are 0640 with the configured group.
Compose adds that group to non-root container users. File-backed Compose secrets
retain host permissions, so declaring container `uid`/`gid` does not repair them.
Real secret generation intentionally requires Linux. Keep this directory on the
trusted Docker host. Reader/worker receive no host-admin secret; only Keycloak
and its setup job receive the bootstrap administrator password.
Neo4j's documented `NEO4J_AUTH` comes from a private `env_file`; Docker
administrators can inspect it. Avoid printing expanded Compose configuration or
collecting unrestricted container inspections.

## Validate, build and start

Pass the generated environment explicitly to avoid loading an unrelated `.env`:

```sh
docker compose --env-file /srv/graphmind-private/runtime/compose.env \
  -f deploy/docker/compose.yaml config --quiet
docker compose --env-file /srv/graphmind-private/runtime/compose.env \
  -f deploy/docker/compose.yaml build
docker compose --env-file /srv/graphmind-private/runtime/compose.env \
  -f deploy/docker/compose.yaml up -d
docker compose --env-file /srv/graphmind-private/runtime/compose.env \
  -f deploy/docker/compose.yaml ps -a
```

The main build context is allowlisted; a prepared context contains eight public
provider/bootstrap inputs only. The application target reuses the hash-required
offline installer with build networking disabled. Keycloak reuses the existing
JDK-21 compiler/provider sources and email theme, then performs its optimized
build offline. The application wheel includes prebuilt frontend assets; no npm
build, package-index access or model download is needed for its installation.
Pulling base images and Debian build packages still requires an approved
registry/mirror. Default component version tags are recorded in Compose; image
digests/OS packages are not yet independently frozen. For release qualification,
record image IDs and set reviewed digest references through `GRAPHMIND_*_IMAGE`
entries in the generated `compose.env`. Image tags do not replace wheel hashes.

The `identity-setup` job applies and reads back canonical user-profile/recovery
policies while preserving existing attributes/listeners. Its administrative
bootstrap grant does not enable reader password grants. The public proxy and
reader wait for successful completion. No human/test reader is automatically
created. Import skips an existing realm; keep its PostgreSQL data and generated
secrets together. Begin registration/login at `https://rag.example.com/`.
Only the GraphMind realm and login resources are public; master/admin/account/
management paths are blocked. Existing host account CLI uses a loopback route.

## Networking, state and host administration

A small `gateway` container owns one network namespace and publishes only
80/443. Caddy's `proxy` serves those ports after private identity setup. All
backend/application containers join that namespace and retain distinct loopback
listeners, preserving existing Chroma/local-inference/proxy checks. This uses no
host networking and publishes no database, model, Chroma or management port.
Internal networking is shared within this installation. Do not scale the
namespace owner or writer; recreate dependent containers together when replacing
the namespace owner.

Issuer/reader loopback hostname entries belong on the gateway, whose hosts/DNS
files are inherited by namespace members. Docker forbids per-member `extra_hosts`,
DNS overrides and published ports in container network mode.

Named volumes retain app metadata/originals/artifacts, Chroma, PostgreSQL,
Neo4j, model-server home and certificates. Reader/worker/CLI share one source/job
authority and inference lock. Chroma alone owns its persistence volume. Models
and imports are separate read-only mounts. Restart policies assist recovery;
Compose dependency checks govern startup, not continuous dependency supervision.

Use the existing host-only CLI while the stack is running:

```sh
docker compose --env-file /srv/graphmind-private/runtime/compose.env \
  -f deploy/docker/compose.yaml run --rm --no-deps admin doctor
docker compose --env-file /srv/graphmind-private/runtime/compose.env \
  -f deploy/docker/compose.yaml run --rm --no-deps admin import \
  /imports/manual.pdf --engines graphmind,tree_navigation,multimodal --enqueue-only
docker compose --env-file /srv/graphmind-private/runtime/compose.env \
  -f deploy/docker/compose.yaml run --rm --no-deps admin status
```

Use `admin wizard` for the existing guided workflow, or `admin accounts list`
and the existing account actions. The worker processes durable queued jobs.
Docker access is host administration; no public upload/web admin/MCP mutation
is added. Caddy access logs stay disabled, and its request-error logger is
excluded because callback URLs can contain authorization codes.

`docker compose ... down` preserves named volumes. `down --volumes` deletes
this installation's persistent data. Volumes are not backups. Secure backup/
restore, revocation-safe rollback and host-reboot qualification remain separate
deferred work. Preserve generated secrets/TLS state alongside their matching data.

## Runtime smoke still required

Retain image IDs, bundle/model identities and any failed attempts. Build/start
an isolated stack, verify health and genuine login/MCP access, ingest one owned
PDF and check all three engine answers/citations/source pages, then perform an
idle container restart and confirm original bytes and current index heads
persist. Coarse reader health is not proof that every engine is ready; inspect
the existing catalogue/status. Keep full quality, stress and recovery deferrals
separate from this bounded smoke.

Public abuse/rate-limit and recovery-mailbox qualification remain release work.
The existing Nginx recovery POST-limit example is not automatically supplied by
the standard Caddy image; review that public proxy policy before production.

References: [Compose services](https://docs.docker.com/reference/compose-file/services/),
[named build contexts](https://docs.docker.com/reference/compose-file/build/),
[Keycloak containers/providers](https://www.keycloak.org/server/containers),
[Neo4j image configuration](https://neo4j.com/docs/operations-manual/current/docker/introduction/),
[Windows support boundary](https://docs.docker.com/desktop/setup/install/windows-install/).

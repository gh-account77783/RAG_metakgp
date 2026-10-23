# Preparing a complete native Ubuntu installation

Use a qualified Python bundle/runtime and separately retained PostgreSQL,
Keycloak, Neo4j, JDK, Ollama and model artifacts. The installer does not download
artifacts, initialize databases, create accounts, change networking or configure
identity. Preserve notices and approved hashes with each artifact.

Choose a new `graphmind-...` namespace in both layouts from `deploy/native/`.
Provision four distinct non-root service accounts, writable state directories
and private configuration using that guide and the vendor initialization tools.
Configure PostgreSQL authentication, optimized Keycloak with the recovery provider,
Neo4j connectors, and GraphMind's existing shared configuration. Never initialize
over a retained cluster. Keep the host account-administration secret separate.

Prepare all seven services together, using the existing service renderers:

```console
python -m tools.ubuntu_installation prepare /private/new-units --application /private/app.json --backends /private/backends.json
```

Review the generated unit files and `installation.json`, then retain its SHA-256
separately. Cross-layout namespace/account/state collisions are rejected, and
reader/worker startup orders after all native backends. No service is activated.
Run effective-account preflight on native Ubuntu as root:

```console
sudo python -m tools.ubuntu_installation preflight /private/new-units --manifest-sha256 REVIEWED_SHA256
```

Preflight checks actual service-user access to binaries, protected configuration,
read-only models and writable state, distinct non-root UIDs, protected file modes
and the existing 512 MiB application disk floor. It reads no configuration secrets.
This floor is not installation capacity: budget retained artifacts, unpacking,
backup, old/new releases and measured workload headroom separately. Inspect free
memory and every configured listener before activation. Preflight does not parse
backend configuration, test authentication or prove model behavior.

For an approved new installation only:

```console
sudo python -m tools.ubuntu_installation install /private/new-units --manifest-sha256 REVIEWED_SHA256
```

Installation repeats preflight, verifies the reviewed bytes with systemd, refuses
existing persistent/vendor/transient units and drop-ins in that namespace, installs
only those seven files and reloads systemd. It does not start or enable anything.
Preserve output and partial files on failure; inspect them before recovery. Use the
separate update procedure for an existing namespace.

Start PostgreSQL first, then Keycloak/Neo4j, Chroma/Ollama, reader and worker. Use
the exact namespace's seven unit names. Run GraphMind's `doctor`, check identity
discovery/JWKS, every engine's readiness and original source/citation access, then
exercise an owned import/replace/delete through the host CLI/wizard. Check actual
jobs and backend stores; reader readiness alone can mean only one engine is ready.
Configure validating HTTPS and exact OAuth callbacks before granting network
access. Enable/reboot only within the approved acceptance scope and verify actual
autostart, update/rollback and protected backup/restore. This tooling is an operator
installation step, not a claim of clean-host or production acceptance.

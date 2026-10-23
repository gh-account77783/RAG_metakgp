# Preparing a complete native Ubuntu installation

Run these module commands from the checkout root, or from the installed
`share/graphmind` operator directory under the qualified Python environment.
The packaged `tools` directory supplies the same renderer and installer modules.

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

For a direct native HTTPS reader, add `ssl_certfile` and `ssl_keyfile` together
to the application layout. Use an unencrypted PEM key protected for the service
account and a certificate chain valid for the configured hostname. The existing
`serve` command passes these to Uvicorn; it validates the chain/key before opening
application state and requires an HTTPS `public_base_url`. Set the matching MCP
audience and OAuth callbacks in the existing identity configuration, keep secure
cookies enabled, and verify clients with the intended trust chain. Do not install
a test CA globally or disable client certificate verification. Keycloak's own
HTTPS/proxy configuration remains a separate vendor input. Certificate renewal
and restart acceptance remain operational checks.

For an explicitly trusted private identity CA, set `identity_ca_file` or
`GRAPHMIND_IDENTITY_CA_FILE` to its PEM certificate bundle in the protected
installation configuration. Identity discovery/token/introspection/admin requests
and ID-token JWKS loading use that same trust input. It adds the selected CA to
the pinned public roots without changing certifi or the host trust store; hostname
and certificate validation remain enabled. Model-provider trust remains unchanged.
Restart affected services when replacing a retained CA file.

## New host configuration

`tools.ubuntu_configuration` generates private configuration from the same two
service layouts and realm template. Its bounded profile uses nine distinct
loopback ports, direct validating HTTPS and local Ollama models. Choose model
identifiers and absolute reranker/visual model paths in a secret-free GraphMind
TOML first; the command does not choose, download or promote models.

Create a JSON profile with these fields:

| Field | Value |
| --- | --- |
| `schema_version` | `1` |
| `application`, `backends` | The complete existing layout objects |
| `ports` | Object with integer `postgresql`, `keycloak_http`, `keycloak_https`, `keycloak_management`, `neo4j_bolt`, `neo4j_http` ports |
| `import_dir` | Dedicated host ingestion directory outside application/backend state and models |
| `identity_ca_file` | Explicit trusted identity CA PEM, outside the new configuration tree |
| `keycloak_certfile`, `keycloak_keyfile` | Retained certificate chain/private key readable by the Keycloak service account |

Use `/etc/NEW-NAMESPACE/reader.toml`, `reader.env`, `keycloak.env` and
`neo4j/` for the corresponding layout fields. Add the reader certificate/key
pair to the application layout. Keep TLS inputs outside this new configuration
directory. They must already have correct service-account access; no global
trust store is modified.

```console
python -m tools.ubuntu_configuration prepare /private/profile.json /private/models.toml /private/new-configuration
sudo python -m tools.ubuntu_configuration install /private/profile.json /private/new-configuration --manifest-sha256 REVIEWED_CONFIGURATION_SHA256
```

Review target paths, roles, permissions and hashes locally without publishing
the generated files. Staging is private and exclusive. Installation refuses an
existing configuration root, verifies the separately reviewed manifest and every
file, and creates only the new configuration tree. The reader receives browser,
resource and source-store credentials; host administration gets a separate
root-only file. Keycloak bootstrap/database credentials and realm import remain
root-only. No human reader account is created. The realm uses existing S256
clients with direct password grants disabled, and the host service identity has
only `query-users`, `view-users`, `manage-users` roles. Google stays disabled and
SMTP remains a separate owner-supplied input.

Initialize fresh backend state with the pinned vendor tools before installing
units. Preserve logs privately and stop on any failure; never retry initialization
over an existing cluster:

1. Create the four accounts with no interactive shell, and their dedicated
   writable state directories. Keep Python/native binaries/models root-owned and
   non-writable to service users. Create application `chroma`, `home`, `cache`,
   separate Ollama home, ingestion staging and PostgreSQL `socket` directories
   with the appropriate service ownership.
2. As the PostgreSQL account, run its qualified `initdb` with `-D DATA`,
   `-L SHARE_DIRECTORY`, `--username OS_ACCOUNT`, `--auth-local=peer`,
   `--auth-host=scram-sha-256`, `--encoding=UTF8`. Set the retained native library
   directory through `LD_LIBRARY_PATH` if required. Copy the staged manifest's
   PostgreSQL `graphmind.conf` into the initialized data directory with PostgreSQL
   ownership/mode0600, then append `include = 'graphmind.conf'` to
   `postgresql.conf`. It remains staged during configuration installation so
   `initdb` sees an empty directory.
3. Start only the new PostgreSQL unit and wait for `pg_isready` on its exact
   socket/port; a successful `systemctl start` alone does not prove readiness.
   Via its private Unix socket and peer OS
   account, create the `graphmind_keycloak` login role without superuser,
   database-creation or role-creation privileges, then its owned
   `graphmind_keycloak` database. Read `KC_DB_PASSWORD` from protected
   `keycloak.env` and send SQL through subprocess stdin; never place credentials
   in command arguments, shell history or public logs. Keep `ON_ERROR_STOP=1`.
4. Use a separately prepared, optimized Keycloak distribution containing the
   pinned recovery provider and theme. Its `data` link must target this
   installation's own writable state. Never redirect a running distribution's
   link or run `build` against shared hardlinked files. Import the protected
   `realm-import.json` with `kc.sh import --optimized --file PRIVATE_FILE
   --override false` and its protected environment before first realm use.
   Make a mode0600 copy readable only by the Keycloak account in its own state
   directory for that command. The explicit optimized flag avoids an implicit
   rebuild; keep the realm stopped during import, as required by the
   [Keycloak import guide](https://www.keycloak.org/server/importExport).
   Start Keycloak, apply/read back
   `deploy/keycloak/graphmind-user-profile.json` through the private admin API,
   and configure recovery using the existing Keycloak guide. Remove temporary
   bootstrap administrator access after setup; keep only protected host account
   administration credentials. Mail/broker acceptance requires those configured
   external services.
5. Create Neo4j state `data`, `logs`, `run`, `import`, `home` directories. Copy
   the qualified distribution's logging XML into its external configuration
   directory with root/service-group ownership and mode0640. Start only the new
   private Neo4j unit. On this fresh database, use the supported driver with
   bootstrap `neo4j` credentials and a parameterized `ALTER CURRENT USER SET
   PASSWORD FROM $old TO $new`, taking `$new` from protected `reader.env`. Verify
   authenticated connectivity. Never use this initialization on retained stores.
6. Run GraphMind's existing `init` and `doctor` as the application service account
   with its configuration, clean environment and a working directory outside
   protected homes. Start Chroma/Ollama and reader/worker in dependency order.
   Use the separate root-only environment for host `accounts` operations. Import
   owned files through `wizard`/`import`, inspect submitted jobs and each enabled
   engine head, and verify protected original-page access with a compatible
   authenticated client. The ordinary reader has no upload or administration API.

This is a host operator workflow over the retained vendors and shared application
commands. It does not provision cloud resources, install global packages, certify
clean-OS installation, or qualify browser, production rights, off-host recovery or
all-platform acceptance. Record the actual native probe and artifact hashes.

## Host-local backup, restore and update

Keep backups and manifests in a root-owned mode0700 directory outside live
state; files containing data/configuration must be mode0600. Inventory and hash
the exact immutable Python/native/model artifacts separately. Back up protected
configuration, realm/database state, TLS keys/CA, unit files/drop-ins and source
state without printing them or adding them to release artifacts. Model weights
and immutable binaries need their retained installation bundles, rather than
another copy in each state snapshot. Capacity must cover the old release, new
release, a consistent snapshot, restore staging and the application disk floor.

For the single-node native layout:

1. Check every submitted job and stop the worker after current work completes.
   Stop the reader, Keycloak, Chroma, Neo4j and PostgreSQL before taking their
   combined filesystem snapshot. Ollama can remain running when models are
   immutable and no inference jobs are active. Preserve UID/GID, modes and every
   file hash. Reject unexpected links/special files. Verify the archive members
   and hashes before reporting backup success, then resume only those previously
   active services in dependency order and check actual readiness.
2. Restore into a separate protected staging tree and verify every member,
   ownership and hash before swapping any live path. Stop all writers again;
   retain the latest live trees for rollback. Keep reader and worker stopped
   while starting restored database/identity backends. Preserve installation
   identity and source/version IDs. An older snapshot can contain an enabled
   account, a live grant or a source that was later deleted.
3. Before reader exposure, reconcile reviewed post-backup account disables,
   credential revocations and source tombstones against the latest retained
   administrative decisions. Use the existing host `accounts` commands and
   `delete --enqueue-only`/worker, rather than direct SQL edits or a second
   authorization policy. Complete cleanup with the reader still stopped. Verify
   deleted originals/old citations are denied, derived stores are cleaned, and
   an unexpired old grant cannot regain access after restore or account re-enable.
   Obtain a fresh grant for a protected source read. Without trustworthy latest
   decisions, keep affected identities/sources quarantined instead of exposing
   the restored installation.
4. Audit all enabled engine heads against the same active source versions,
   original-byte hashes and actual native stores. Check the shared application
   manifest; a healthy HTTP listener alone is insufficient. Only then start the
   reader and verify authenticated catalogue/fetch/page access. Retain failure
   receipts and the previous live trees; roll back the swap if reconciliation or
   audit fails. Never copy private backup contents into public test evidence.

For a software update, qualify a new immutable Python environment from the
reviewed offline bundle, then use `tools.ubuntu_release_files` to prepare the
three Python-service overrides. Review hashes, stop affected writers and take
a consistent protected snapshot first. Apply the reviewed overrides, reload
systemd, restart Chroma then reader/worker and verify source/index/authentication
checks. Exercise rollback to the retained prior environment and reapply the
candidate. This procedure assumes compatible state; schema or native database
upgrades need their separately reviewed migration/restore procedure.

A host-local restore does not prove recovery after host loss. Off-host encrypted
backup, separate-target recovery, certificate renewal and each required native
platform need their own authorization and evidence. Do not promote a release
from a filesystem copy or from deferred gates alone.

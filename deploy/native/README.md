# Ubuntu application service files

`tools/ubuntu_service_files.py` renders one reader/worker/Chroma/Ollama unit set
from a nonsecret JSON layout. It does not download, create accounts, install,
enable, start or replace services. `tools.ubuntu_backend_files` renders companion
PostgreSQL/Keycloak/Neo4j units using the same isolation and process-limit renderer.
Backend configuration, HTTPS and backup/update procedures remain separate. These files alone are
not a qualified installation or reboot/restore procedure.

Start from `ubuntu.example.json`, choose an unused `graphmind-...` namespace and
three unused ports, and point to retained, verified native artifacts. Keep the
Python environment and native binaries in an immutable release directory. Use
an existing dedicated non-root account/group with a non-login shell; it must
read the release, configuration and retained model weights, and own application
data, its `chroma` directory and the separate Ollama home. Do not put these paths
under `/home` or `/root`: the units protect home directories. Provision these
directories and restrictive permissions before starting services.
Reader/worker home and library caches are explicitly inside application data;
create those directories for the service account as well. Retained model weights
stay in the separate read-only model directory.

The example includes explicit per-service CPU/memory caps. Choose caps for the
measured host and model workload; caps are not reservations and their sum does
not establish that concurrent processes fit in RAM. Qualify co-location and
dependency recovery before treating the layout as an accepted capacity profile.

Keep browser/resource client credentials in the protected dotenv file; omit
the host account-administration secret from the reader/worker configuration.
The units contain file paths rather than secrets. Configure retained reranker
and visual model paths in the same GraphMind configuration. Ollama model storage
is read-only to the service: populate and verify it before activation. The
units force loopback Chroma HTTP mode and same-host local inference, with one
loaded model and offline Hugging Face loading. The application still uses its
shared process lock for model admission and fenced durable ingestion.

```console
python tools/ubuntu_service_files.py /private/layout.json /private/new-service-files --verify
```

`--verify` runs native `systemd-analyze verify`; the executable paths must exist
on that Ubuntu host. An existing output directory is refused. The generated
manifest hashes the unit files; preserve failed verification logs and inspect
the files before installing them. Verification does not establish permissions,
available ports, dependency readiness, authentication or model behavior.

For native backends, start from `ubuntu.backends.example.json` and use the same
service namespace as the application layout:

```console
python -m tools.ubuntu_backend_files /private/backends.json /private/new-backend-files --verify
```

Each backend needs its own existing non-root account and non-overlapping state
directory. Keep verified binaries/JDKs immutable and outside protected homes.
PostgreSQL runs its native `postgres` process with the existing data directory;
`library_dir` supplies only that process's private native libraries. Configure
its loopback listener, socket directory and authentication before activation.
Initialize a new cluster once with vendor tools; never initialize over retained
data. PostgreSQL receives SIGINT for fast, orderly shutdown.

Keycloak uses an already optimized native distribution, including the qualified
GraphMind recovery provider. Its protected environment file supplies database
and identity configuration; the renderer neither reads that file nor copies its
values into units. Place writable Keycloak data under its state directory and
configure the release's data link accordingly. It requires the same namespace's
PostgreSQL service. Neo4j uses `console`, the explicit JDK and `NEO4J_CONF`;
configure data/log/run/import directories under its state directory, loopback
connectors and initial authentication before first startup. Preserve vendor
licenses/notices and qualify artifact versions independently of these layouts.

Keep passwords/client secrets out of both JSON layouts. Configure the reader's
existing shared identity and graph connection to these services, and add explicit
`Wants`/`After` dependencies on them for the reader/worker as appropriate. The
renderers do not change application configuration, permissions or service state.

For an explicitly approved installation, install only that reviewed namespace's
unit files into `/etc/systemd/system`, reload systemd, and start its Chroma and
Ollama units before its reader and worker. Enable only those reviewed units when
startup/reboot tests are authorized. Do not overwrite another installation's
units or data. The reader listens privately; configure the HTTPS proxy and exact
OAuth callbacks before exposing it. Test dependency loss, active-source
visibility, graceful stop and restoration before relying on startup ordering.

SIGTERM goes to the parent first (`KillMode=mixed`) so the worker can finish its
fenced job. After 2000 seconds, systemd may kill remaining processes; an
interrupted job must reconcile on restart. Adjust that timeout to the measured
bounded ingestion workload. Reader/worker data are writable; release/configuration
and model weights remain protected by the filesystem namespace and ordinary
permissions. This is process hardening, not a parser security sandbox.

Native Windows service installation remains a separate target; these systemd
files do not substitute for Windows 11 or Server 2025 acceptance.

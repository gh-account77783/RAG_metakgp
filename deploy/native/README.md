# Ubuntu application service files

`tools/ubuntu_service_files.py` renders one reader/worker/Chroma/Ollama unit set
from a nonsecret JSON layout. It does not download, create accounts, install,
enable, start or replace services. Keycloak/PostgreSQL, Neo4j, HTTPS and their
backup/update procedures must be configured separately. These files alone are
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

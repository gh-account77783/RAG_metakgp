# Native Windows service installation

These commands prepare a new, isolated seven-service SCM installation on Windows
11 or Windows Server 2025. They do not establish native acceptance by themselves.
Use a current, independently verified CPython 3.13 runtime and a fresh complete
offline release bundle. Revoked historical LiteLLM 1.107.0 bundles must not be
used. Retain publisher checksums, vendor licenses/notices and installation receipts.
Never use a system Python/PATH installation as a mutable application release.

`windows_service_files.py` renders nonsecret service specifications. Start from
`deploy/native/windows.example.json`. Choose unused ports and a unique
`graphmind-...` namespace. Supply retained, hash-verified PostgreSQL, JDK 21,
optimized Keycloak with the existing recovery provider, Neo4j and Ollama artifacts.
Backend command arrays invoke native executables directly; adapt their Java
options to the qualified distribution's vendor launcher. The example is a
configuration starting point, not a claim that every vendor version starts with
identical JVM options. No shell, global installer or alternate account authority
is introduced. Initialize new databases once through the existing vendor tools;
never initialize over retained state. Configure backend loopback listeners and
authentication separately, using the existing Keycloak realm/profile contracts.

Install the optional `windows-service` extra into a separate, immutable maintenance
environment from a publisher-verified offline bundle under the existing constraints.
It supplies pinned `pywin32==312` only on Windows. Keep the maintenance interpreter
and `windows_service_host.py` plus its sibling `windows_service_files.py` outside
replaceable application releases. All service accounts need read/execute access
to that runtime and these scripts. Do not run pywin32's global post-install script.
The host runs ordinary `python.exe` and the SCM dispatcher directly; it does not
copy DLLs into Windows or register a global Python service host.

```powershell
python -I -B tools/windows_service_files.py C:\private\layout.json C:\private\reviewed-services
python -I -B tools/windows_installation.py verify C:\private\reviewed-services --manifest-sha256 REVIEWED_SHA256
python -I -B tools/windows_installation.py install C:\private\reviewed-services --manifest-sha256 REVIEWED_SHA256 --live-spec-dir C:\GraphMind\services --host-python C:\GraphMind\maintenance\Scripts\python.exe --host-script C:\GraphMind\maintenance-tools\windows_service_host.py
```

Review the generated files and independently retain the SHA256 of
`installation.json` before installation. The installer rejects an existing SCM
namespace and active specification directory. It creates seven stopped, manual
services running as separate `NT SERVICE\graphmind-NAME-ROLE` virtual accounts.
It protects the new specification directory/files for SYSTEM, Administrators and
the corresponding service SID. It preserves partial failures for inspection and
never automatically removes an earlier service. Installation needs an elevated
operator; application processes do not use LocalSystem or a human password.

Before activation, provision restrictive NTFS ACLs on the named installation
paths. Grant each service SID read/execute on its immutable runtime, vendor inputs
and model files, read on only its protected configuration, and modify on its own
state directory. Reader and worker share application state; Chroma alone owns
its nested persistence directory. Application/Chroma/Ollama/backend state roots
and model storage must be separate as validated by the renderer. Each state
directory needs its own writable `tmp` child. Keep release/config/model files
unwritable by service identities; do not inherit broad Users/Authenticated Users
permissions on secrets, logs or state. Verify effective access under the actual
service identities. The renderer's path checks are not an NTFS ACL audit.

The backend `environment_file` is a protected JSON object of uppercase names and
string values. The host loads it only in that backend. Reader/worker use the
existing `--config`/`--dotenv` application loaders; never supply the host account
administration secret there. The host discards inherited operator/Python/cloud
environment values and rejects admin-secret or interpreter overrides. Explicit
environment objects and backend command arrays are nonsecret review inputs;
keep passwords in protected configuration files. Child stdout/stderr append to
ROLE.log under that service's protected state directory; these logs are private.

Start PostgreSQL, Keycloak, Neo4j, Ollama and Chroma, then the reader; keep the
worker stopped during configuration/readiness checks. SCM dependencies establish
process ordering only. Check actual database/identity connectivity and all enabled
engine heads, original files and authenticated browser/MCP behavior before enabling
work. Explicitly select local inference, retain verified offline visual weights
and Ollama models, and disable hosted fallback. Configure native HTTPS with the
existing issuer/callback/CA rules before exposing the reader. No firewall, DNS,
machine trust store, global environment or listener binding is changed by these
commands. Enable automatic startup only after the owned installation passes.

The host creates a suspended child, assigns it to a memory-bounded Windows Job,
then resumes it. Closing the job contains the full owned process tree. A stop
first runs an optional bounded vendor stop command (PostgreSQL uses fast pg_ctl
shutdown), then sends CTRL+C through the service's private console and waits for
the explicit timeout. The Python worker finishes its fenced job through its
existing SIGINT handler. Java shutdown hooks handle CTRL+C. A timeout terminates
only the owned job and records a failure in the event log; interrupted jobs must
reconcile through the existing executor. Unexpected child exit is an SCM failure.
Job caps are limits, not RAM reservations or evidence of co-location capacity.

Use the companion Windows operations procedure for stopped consistent snapshots
and Python release update/rollback. Keep active service specifications writable
only to administrators; update their protected copies while the corresponding
services are stopped and retain the prior reviewed artifacts. Qualify actual
stop, dependency loss, update/rollback, restore and reboot on each Windows target.
Do not copy protected backups, keys, tokens, realm dumps or credentials into
public release receipts.

The service host follows the upstream [pywin32 SCM dispatcher](https://github.com/mhammond/pywin32/blob/main/win32/Lib/win32serviceutil.py)
and Windows [console-control](https://learn.microsoft.com/en-us/windows/console/generateconsolectrlevent)
and [Job Object](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
contracts. Platform tests must still qualify the actual runtime and vendor
processes; offline contract tests do not start native services.

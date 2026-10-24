# Native Windows update and protected recovery

Run `python -m tools.windows_operations` from the checkout or installed
`share/graphmind` directory, using the stable maintenance Python with the
reviewed pywin32 service host. Run native operations in an elevated operator
session. They enable backup, restore and security privileges in that process;
they do not change global policy, firewall rules, accounts or networking.

The seven service names and commands come from the separately reviewed
`installation.json` and service JSON files produced by `windows_service_files`.
Retain the approved SHA-256 independently. Keep the live service-spec directory
read-only to service accounts, outside every mutable state/configuration tree
that recovery will replace. The SCM host executable/script and maintenance
Python remain immutable and separate from application releases.

## Stop and snapshot

First verify no active/waiting reader requests, nonterminal jobs or account
administration operations. Freeze host imports and account changes for the
snapshot. Retain exact service start modes, protected configuration, binary and
model inventories, installed-release hashes and current source/index identities.
Use only the installation's explicitly approved namespace.

For a public Caddy proxy, always supply `--proxy-service NAMESPACE-proxy` to
every native operation. Stop disables and stops it before the seven services,
and reports its previous numeric start type. Subsequent operations require it
to remain STOPPED and DISABLED. The tool never reenables it. This also prevents
old restored Keycloak grants from becoming publicly usable during recovery.

```powershell
python -m tools.windows_operations stop C:\GraphMindP7\prepared --manifest-sha256 REVIEWED_SHA256 --proxy-service graphmind-p7-proxy
```

Worker/reader stop before stores; the tool waits for SCM STOPPED and successful
exit status. A timeout or forced service termination is a failed quiesce,
requiring inspection before backup. The tool does not kill arbitrary processes.

Prepare a protected JSON object mapping labels to existing absolute directories.
Required labels are `application`, `configuration`, `postgresql`, `keycloak` and
`neo4j`; add dedicated `inputs` and `ollama_home` roots where needed. Include
every service's mutable state and all referenced GraphMind/backend configuration,
TLS inputs and private identity material. Roots must be disjoint and exclude
software releases and model weights. If a referenced TLS/CA file lives elsewhere,
include its dedicated containing directory without broadening the snapshot to
unrelated host data. Keep live service specs separate and retain their reviewed
immutable preparation directory as the service-definition recovery input.

```powershell
python -m tools.windows_operations backup C:\GraphMindP7\prepared --manifest-sha256 REVIEWED_SHA256 --roots C:\GraphMindP7\private\recovery-roots.json --directory C:\GraphMindP7\backups\snapshot-001 --proxy-service graphmind-p7-proxy
```

Snapshot directories are exclusively created with only Administrators/SYSTEM
access. `robocopy /COPY:DAT /DCOPY:DAT` streams ordinary files and preserves
timestamps into that private tree. Original owner/group/DACL/SACL are recorded
in its protected manifest rather than exposing snapshot copies to service
accounts. Source/copy SHA-256, sizes, modification times and source ACLs are
verified; source state must remain unchanged and all services stopped throughout.
Reparse points, hard links and alternate file data streams are rejected. Budget
the full copy plus at least 512 MiB free; this reserve is a minimum, not measured
workload headroom. Retain `snapshot_sha256` outside the snapshot on this host.
No state, manifest, credential or private key is an ordinary downloadable receipt.

## Sealed restore

Before restoring an older snapshot, retain the latest terminal account actions
and source deletions separately on the host, using the existing host CLI and
source/job authority. Preserve the current service definitions/start modes and
latest protected state. Stop/disable the public proxy and stop all seven services
again. The tool never restores SCM registrations or creates service identities.

```powershell
python -m tools.windows_operations restore C:\GraphMindP7\prepared --manifest-sha256 REVIEWED_SHA256 --directory C:\GraphMindP7\backups\snapshot-001 --sha256 SNAPSHOT_SHA256 --seal C:\GraphMindP7\service-specs\RESTORE_SEALED.json --proxy-service graphmind-p7-proxy
```

Use the **actual live service-spec directory** for `--seal`. Reader/worker hosts
refuse startup while that file exists, including after reboot. Its directory must
remain outside restored roots. Caddy uses its native SCM implementation, so it
must remain disabled and stopped independently of the file guard.

The tool verifies snapshot contents before mutation, checks scratch capacity per
volume, creates the seal exclusively, and copies each root to a sibling
`.restore-stage`. It restores and verifies original security descriptors on
scratch, then confirms services remain stopped. Each current root is renamed to
`.pre-restore` and the verified scratch root replaces it. These per-root renames
are not a cross-volume transaction. Failure leaves services stopped, the seal,
partial stages and every retained pre-restore tree for inspection. Existing stage
or retained names refuse another restore; never remove them blindly to retry.

Start only private dependencies for reconciliation. Through the canonical
`graphmind accounts disable/delete` and `graphmind delete` commands, replay all
latest disable/deletion actions against the restored stores; do not edit SQLite,
Neo4j, Chroma or Keycloak tables as a second authority. Host job execution can
finish required tombstone cleanup while the reader/worker service hosts remain
sealed. Verify old unexpired grants are denied, deleted sources are denied,
original source hashes and every expected current engine head remain intact,
and the protected configuration/security descriptors match the intended state.
Only after these checks may the operator retain the reconciliation receipt,
remove the live seal, restore exact intended service modes, start reader/worker,
and finally restore the recorded proxy mode/start it. Use actual authenticated
read/source checks before claiming successful recovery. The helper does not
assert that reconciliation has occurred or automatically expose restored state.

## Immutable Python update and rollback

Install and qualify a clean hash-verified offline bundle in a different immutable
release directory; retain the previous release and its bundle. Prepare a second
complete layout/spec directory using the same configuration, models, state,
accounts and ports, changing only the reader/worker Python executable and Chroma
executable to the new closure. Native/database/model/schema changes require
their separately qualified migration and consistent snapshot.

```powershell
python -m tools.windows_operations prepare-update C:\GraphMindP7\prepared --manifest-sha256 OLD_REVIEWED_SHA256 --candidate C:\GraphMindP7\prepared-new --candidate-sha256 NEW_REVIEWED_SHA256 --directory C:\GraphMindP7\release-changes\r2
```

Review `update.json` and retain its SHA-256. Stop the installation and take a
consistent protected snapshot. `update` checks live specs exactly match the
reviewed old bytes, prepares new files with the existing live ACLs, and replaces
only the three executable specifications. It never changes SCM executable,
service identity, dependencies, configuration/state paths, or service modes.

```powershell
python -m tools.windows_operations update C:\GraphMindP7\prepared --manifest-sha256 OLD_REVIEWED_SHA256 --directory C:\GraphMindP7\release-changes\r2 --sha256 UPDATE_SHA256 --live-specs C:\GraphMindP7\service-specs --proxy-service graphmind-p7-proxy
```

All services remain stopped. Start private dependencies, then application
services, and verify executable paths, actual source/index/identity checks and
an owned import/replace/delete. Restore public proxy mode only after validation.
Exercise `rollback` with the same reviewed update directory and SHA-256 while
all services/proxy are quiesced; it requires live specs to match the candidate
and restores the reviewed old bytes. Reapply only after successful rollback
checks. Partial replacement or `.pending` files fail visibly and require
inspection, rather than an automatic retry/restart. Code rollback is safe only
when persisted state remains compatible; otherwise use sealed state recovery
and replay the latest security/deletion actions before exposure.

These portable contracts do not qualify actual Windows service stop, ACL/SACL,
reboot, inference, update or restore behavior. Record those on the intended
Windows 11/Server 2025 target with the exact installed artifact and owned inputs.

# Native acceptance boundary

Ubuntu 24.04, Windows 11 and Windows Server 2025 must each qualify actual
installation, private bindings, service-account files/model caches, browser/MCP
access, ingestion, restart/reboot, update/rollback and consistent restore.

This directory reserves the native acceptance harness boundary. No placeholder
or skipped test is supplied as evidence that those gates have passed. Existing
explicit real-service commands remain in `tools/` and procedures in `deploy/`.
Hardware, privileged changes, live accounts and provider calls require their
corresponding scope/access/budget; ordinary offline discovery does not run them.

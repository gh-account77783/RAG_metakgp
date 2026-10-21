# Configuration inputs

`graphmind.example.toml` and `.env.example` are secret-free templates. Copy them
explicitly into the intended private runtime configuration; do not overwrite an
existing `.env` or put credentials in tracked examples. The owner's existing
root `.env` and product configuration precedence are unchanged.

`constraints/` contains the existing exact Python 3.13 dependency snapshots.
Paths changed during repository organization; package versions did not.
These are not yet the planned complete hash-verified release lock/wheelhouse.

The root `.mcp.json` intentionally remains at the root for Claude Code project
discovery. Keycloak and proxy configuration assets remain under `deploy/`.

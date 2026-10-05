#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'EOF'
deploy.sh is retired because it installs the superseded static-key/Google-token
server and would bypass the P5/P6 account policy.

For local candidate review, configure the identity settings described in
AUTH_AND_MCP.md and run `graphmind serve`. The production Ubuntu installer is a
P7 deliverable and this script must not be used for an internet-facing host.
EOF
exit 1

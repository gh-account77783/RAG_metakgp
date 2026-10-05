"""Retired compatibility entry point for the pre-P5 MCP prototype.

The old server accepted static bearer keys and Google ID tokens directly. That
would bypass GraphMind's active-account, audience, scope, and revocation checks,
so it must not remain runnable beside the product server.
"""

from __future__ import annotations


def main() -> None:
    raise SystemExit(
        "The legacy MCP server is retired. Configure Keycloak and run "
        "`graphmind serve`; see AUTH_AND_MCP.md."
    )


if __name__ == "__main__":
    main()

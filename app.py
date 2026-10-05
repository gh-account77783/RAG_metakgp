"""Retired compatibility entry point for the unauthenticated Streamlit prototype."""

from __future__ import annotations


def main() -> None:
    raise SystemExit(
        "The legacy Streamlit UI is retired because it bypasses reader authorization. "
        "Configure Keycloak and run `graphmind serve`; see AUTH_AND_MCP.md."
    )


if __name__ == "__main__":
    main()

"""Independent official-SDK network client for a pre-registered reader grant.

Obtain the grant through the client's real PKCE login, then supply it privately
via GRAPHMIND_TEST_ACCESS_TOKEN. This verifies network tool/resource behavior,
not the client's interactive OAuth flow. Never pass tokens in argv or URLs.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from urllib.parse import urlparse

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from httpx2 import AsyncClient


async def run(url: str, question: str) -> None:
    token = os.environ.get("GRAPHMIND_TEST_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError("Missing private reader credential")
    async with AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                if {item.name for item in tools.tools} != {"search_documents", "fetch_document", "ask"}:
                    raise RuntimeError("Unexpected tool contract")
                found = await session.call_tool("search_documents", {"query": question})
                if found.is_error or not found.structured_content:
                    raise RuntimeError("Search did not return structured evidence")
                evidence = found.structured_content.get("results", [])
                if not evidence:
                    raise RuntimeError("Acceptance fixture was not retrieved")
                document = evidence[0]["document_id"]
                fetched = await session.call_tool("fetch_document", {"document_id": document})
                asked = await session.call_tool("ask", {"question": question})
                if fetched.is_error or asked.is_error:
                    raise RuntimeError("Fetch or ask failed")
                if not asked.structured_content or asked.structured_content.get("outcome") != "answer":
                    raise RuntimeError("Fixture answer did not produce a supported answer")
                if not asked.structured_content.get("citations"):
                    raise RuntimeError("Fixture answer omitted supporting citations")
                await session.read_resource(f"graphmind://documents/{document}")
                print("Network initialize/list/search/fetch/ask/resource passed; content and credentials are not printed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--question", default="What is the acceptance fixture launch code?")
    args = parser.parse_args()
    parsed = urlparse(args.url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        parser.error("Use HTTPS or explicit loopback HTTP development")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("Credential-bearing or query-bearing URLs are forbidden")
    try:
        asyncio.run(run(args.url, args.question))
    except Exception as exc:
        print(f"Remote MCP acceptance incomplete ({type(exc).__name__}); no credential/content details printed.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

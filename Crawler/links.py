"""Pure URL rules used by the MetaKGP crawler."""

from typing import Optional
from urllib.parse import urljoin


BASE_URL = "https://wiki.metakgp.org"
EXCLUDED_NAMESPACES = frozenset({
    "Special", "File", "Talk", "User", "Category", "Template", "Help", "MediaWiki"
})


def content_url(href: str) -> Optional[str]:
    """Return an absolute MetaKGP content URL, or ``None`` for non-content links."""
    if not (href.startswith("/w/") or href.startswith("/wiki/")):
        return None
    page_name = href.lstrip("/").split("/", 1)[-1]
    namespace = page_name.split(":", 1)[0] if ":" in page_name else ""
    if namespace in EXCLUDED_NAMESPACES:
        return None
    return urljoin(BASE_URL, href)

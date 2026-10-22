"""One pinned public trust bundle for application HTTPS clients."""
from functools import lru_cache
import ssl

import certifi


@lru_cache(maxsize=1)
def verified_context() -> ssl.SSLContext:
    # Passing cafile explicitly avoids reintroducing removed roots through a
    # different host trust store. Keep the published bundle byte-for-byte intact.
    return ssl.create_default_context(cafile=certifi.where())

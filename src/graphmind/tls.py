"""One pinned public trust bundle for application HTTPS clients."""
from functools import lru_cache
import ssl

import certifi


@lru_cache(maxsize=4)
def verified_context(additional_ca_file: str | None = None) -> ssl.SSLContext:
    # Passing cafile explicitly avoids reintroducing removed roots through a
    # different host trust store. Keep the published bundle byte-for-byte intact.
    context=ssl.create_default_context(cafile=certifi.where())
    if additional_ca_file:
        context.load_verify_locations(cafile=additional_ca_file)
    return context

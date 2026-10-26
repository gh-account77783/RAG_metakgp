"""Render the canonical new-install realm shared by native and container setup."""
from __future__ import annotations

import json
import re


def render_realm(template: str, values: dict[str, str]) -> dict:
    """Substitute JSON string values without interpreting them as JSON or code."""
    def expand(value):
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, str):
            return re.sub(r'\$\{([A-Z0-9_]+)\}', lambda match: values[match[1]], value)
        return value

    realm = expand(json.loads(template))
    realm['users'] = [{
        'username': 'service-account-graphmind-admin', 'enabled': True,
        'serviceAccountClientId': 'graphmind-admin',
        'clientRoles': {'realm-management': ['query-users', 'view-users', 'manage-users']},
    }]
    return realm

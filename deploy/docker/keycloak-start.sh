#!/bin/bash
set -euo pipefail
# Bash builtins avoid depending on extra utilities in the vendor image.
export KC_DB_PASSWORD="$(< /run/secrets/postgres_password)"
export KC_BOOTSTRAP_ADMIN_PASSWORD="$(< /run/secrets/keycloak_bootstrap_password)"
exec /opt/keycloak/bin/kc.sh start --optimized --import-realm

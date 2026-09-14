#!/usr/bin/env bash
# Test the committed P1-P3 GraphMind candidate on an Ubuntu 24.04 EC2 host.
# This is an isolated validation harness, not a production deployment script.

set -Eeuo pipefail
IFS=$'\n\t'

NEO4J_IMAGE="neo4j:2026.04.0"
NEO4J_CONTAINER="graphmind-test-neo4j-2026-04-0"
NEO4J_VOLUME="graphmind_test_neo4j_2026_04_0"
WITH_HOSTED_MODEL=0
SKIP_SYSTEM_PACKAGES=0
CURRENT_STEP="startup"

usage() {
    cat <<'EOF'
Usage: bash test-deploy.sh [options]

Options:
  --with-hosted-model    Also call the configured hosted answer model.
                         First add OLLAMA_API_KEY to the private env file
                         printed by the script.
  --skip-system-packages Skip apt and Docker service setup.
  -h, --help             Show this help.

Run this script as the normal SSH user, not as root. It uses sudo only for
Ubuntu packages and Docker. It does not expose a public service or modify AWS
security groups.
EOF
}

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

on_error() {
    local exit_code=$?
    printf '\nFAILED during: %s (exit %s)\n' "$CURRENT_STEP" "$exit_code" >&2
    printf 'Review the run log path printed near the start of the script.\n' >&2
    exit "$exit_code"
}
trap on_error ERR

while (($#)); do
    case "$1" in
        --with-hosted-model)
            WITH_HOSTED_MODEL=1
            ;;
        --skip-system-packages)
            SKIP_SYSTEM_PACKAGES=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "Unknown option: $1"
            ;;
    esac
    shift
done

[[ ${EUID:-$(id -u)} -ne 0 ]] || fail "Run as the normal SSH user, not root."
[[ -r /etc/os-release ]] || fail "Cannot identify the operating system."

# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == "ubuntu" && ${VERSION_ID:-} == "24.04" ]] || \
    fail "This test target requires Ubuntu 24.04 LTS; found ${PRETTY_NAME:-unknown}."

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null) || \
    fail "test-deploy.sh must be run from a Git checkout."
[[ $SCRIPT_DIR == "$REPO_ROOT" ]] || fail "Keep test-deploy.sh at the repository root."
cd "$REPO_ROOT"

[[ -f src/graphmind/parsing.py && -f tests/test_graphmind_p3.py ]] || \
    fail "This checkout does not contain the P3 parser and tests. Commit and pull P3 first."

GIT_HASH=$(git rev-parse HEAD)
GIT_SHORT=$(git rev-parse --short=12 HEAD)
[[ -z $(git status --porcelain --untracked-files=all) ]] || \
    fail "The checkout is dirty. Test an exact committed candidate from a clean checkout."

STATE_ROOT="${GRAPHMIND_TEST_STATE_DIR:-$HOME/.local/state/graphmind-test-deploy}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-${GIT_SHORT}-$$"
RUN_ROOT="$STATE_ROOT/runs/$RUN_ID"
CANDIDATE_ROOT="$RUN_ROOT/candidate"
SECRETS_DIR="$STATE_ROOT/secrets"
DOTENV_FILE="$SECRETS_DIR/graphmind-test.env"
DOCKER_ENV_FILE="$SECRETS_DIR/neo4j-docker.env"
PASSWORD_FILE="$SECRETS_DIR/neo4j-password"
BUILD_VENV="$STATE_ROOT/venvs/build-${GIT_SHORT}"
WHEEL_VENV="$RUN_ROOT/wheel-venv"
ARTIFACT_DIR="$RUN_ROOT/artifacts"
INPUT_DIR="$RUN_ROOT/input"
RUNTIME_DIR="$RUN_ROOT/runtime"
CONFIG_FILE="$RUN_ROOT/graphmind-test.toml"
LOG_FILE="$RUN_ROOT/test-deploy.log"
ENVIRONMENT_REPORT="$RUN_ROOT/environment.txt"

umask 077
mkdir -p "$RUN_ROOT" "$CANDIDATE_ROOT" "$SECRETS_DIR" "$ARTIFACT_DIR" "$INPUT_DIR"
chmod 700 "$STATE_ROOT" "$RUN_ROOT" "$SECRETS_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

printf 'GraphMind Ubuntu test deployment\n'
printf 'Commit: %s\n' "$GIT_HASH"
printf 'Run directory: %s\n' "$RUN_ROOT"
printf 'Log: %s\n' "$LOG_FILE"

CURRENT_STEP="exporting the exact committed candidate"
git archive "$GIT_HASH" | tar -xf - -C "$CANDIDATE_ROOT"
[[ -f $CANDIDATE_ROOT/src/graphmind/parsing.py ]] || \
    fail "The committed archive does not contain P3."

if ((SKIP_SYSTEM_PACKAGES == 0)); then
    CURRENT_STEP="installing Ubuntu test prerequisites"
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
        ca-certificates curl docker.io git openssl python3 python3-pip python3-venv
    sudo systemctl enable --now docker
fi

for command_name in curl git openssl python3 sudo tar; do
    command -v "$command_name" >/dev/null || fail "Required command is missing: $command_name"
done
sudo docker version >/dev/null

CURRENT_STEP="recording host configuration"
INSTANCE_TYPE="unknown"
IMDS_TOKEN=$(curl -fsS --max-time 2 -X PUT \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
    http://169.254.169.254/latest/api/token 2>/dev/null || true)
if [[ -n $IMDS_TOKEN ]]; then
    INSTANCE_TYPE=$(curl -fsS --max-time 2 \
        -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
        http://169.254.169.254/latest/meta-data/instance-type 2>/dev/null || true)
    [[ -n $INSTANCE_TYPE ]] || INSTANCE_TYPE="unknown"
fi
{
    printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'git_commit=%s\n' "$GIT_HASH"
    printf 'instance_type=%s\n' "$INSTANCE_TYPE"
    printf 'os=%s\n' "$PRETTY_NAME"
    printf 'architecture=%s\n' "$(uname -m)"
    printf 'kernel=%s\n' "$(uname -sr)"
    printf 'python=%s\n' "$(python3 --version 2>&1)"
    printf 'neo4j_image=%s\n' "$NEO4J_IMAGE"
    printf '\nCPU\n'
    lscpu
    printf '\nMEMORY\n'
    free -h
    printf '\nFILESYSTEM\n'
    df -hT "$HOME"
    printf '\nBLOCK DEVICES\n'
    lsblk
    printf '\nGPU\n'
    if command -v nvidia-smi >/dev/null; then nvidia-smi; else printf 'none detected\n'; fi
} >"$ENVIRONMENT_REPORT" 2>&1
cat "$ENVIRONMENT_REPORT"
if [[ $INSTANCE_TYPE != "unknown" && $INSTANCE_TYPE != "t3.large" ]]; then
    printf 'WARNING: Expected experimental t3.large; metadata reports %s.\n' "$INSTANCE_TYPE"
fi

CURRENT_STEP="preparing private Neo4j test credentials"
if [[ ! -s $PASSWORD_FILE ]]; then
    openssl rand -hex 24 >"$PASSWORD_FILE"
fi
chmod 600 "$PASSWORD_FILE"
NEO4J_PASSWORD=$(<"$PASSWORD_FILE")
[[ ${#NEO4J_PASSWORD} -ge 8 ]] || fail "Generated Neo4j password is unexpectedly short."

printf 'NEO4J_AUTH=neo4j/%s\n' "$NEO4J_PASSWORD" >"$DOCKER_ENV_FILE"
chmod 600 "$DOCKER_ENV_FILE"
if [[ ! -f $DOTENV_FILE ]]; then
    {
        printf 'NEO4J_URI="bolt://localhost:7687"\n'
        printf 'NEO4J_USERNAME="neo4j"\n'
        printf 'NEO4J_PASSWORD="%s"\n' "$NEO4J_PASSWORD"
        printf 'NEO4J_DATABASE="neo4j"\n'
        printf 'OLLAMA_BASE_URL="https://ollama.com"\n'
        printf 'GRAPHMIND_ANSWER_PROVIDER="ollama"\n'
        printf 'GRAPHMIND_ANSWER_MODEL="gemma4:31b-cloud"\n'
        printf 'OLLAMA_API_KEY=""\n'
    } >"$DOTENV_FILE"
fi
chmod 600 "$DOTENV_FILE"

CURRENT_STEP="starting pinned Neo4j test container"
if sudo docker container inspect "$NEO4J_CONTAINER" >/dev/null 2>&1; then
    EXISTING_IMAGE=$(sudo docker inspect --format '{{.Config.Image}}' "$NEO4J_CONTAINER")
    [[ $EXISTING_IMAGE == "$NEO4J_IMAGE" ]] || \
        fail "Existing $NEO4J_CONTAINER uses $EXISTING_IMAGE, expected $NEO4J_IMAGE."
    sudo docker start "$NEO4J_CONTAINER" >/dev/null
else
    sudo docker run -d \
        --name "$NEO4J_CONTAINER" \
        --restart unless-stopped \
        --publish 127.0.0.1:7474:7474 \
        --publish 127.0.0.1:7687:7687 \
        --env-file "$DOCKER_ENV_FILE" \
        --env NEO4J_server_memory_heap_initial__size=512m \
        --env NEO4J_server_memory_heap_max__size=1g \
        --env NEO4J_server_memory_pagecache_size=512m \
        --volume "$NEO4J_VOLUME:/data" \
        "$NEO4J_IMAGE" >/dev/null
fi

NEO4J_READY=0
for _ in $(seq 1 90); do
    if (echo >/dev/tcp/127.0.0.1/7687) >/dev/null 2>&1; then
        NEO4J_READY=1
        break
    fi
    sleep 2
done
if ((NEO4J_READY == 0)); then
    sudo docker logs --tail 200 "$NEO4J_CONTAINER" || true
    fail "Neo4j did not listen on localhost:7687 within 180 seconds."
fi

CURRENT_STEP="creating build environment"
if [[ ! -x $BUILD_VENV/bin/python ]]; then
    python3 -m venv "$BUILD_VENV"
fi
BUILD_PYTHON="$BUILD_VENV/bin/python"
"$BUILD_PYTHON" -m pip install --upgrade pip
"$BUILD_PYTHON" -m pip install --upgrade "${CANDIDATE_ROOT}[test]"

CURRENT_STEP="running source candidate offline tests"
cd "$CANDIDATE_ROOT"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH= \
    "$BUILD_PYTHON" -B -m unittest discover -s tests -v

CURRENT_STEP="building wheel and source archive"
"$BUILD_PYTHON" -m build --outdir "$ARTIFACT_DIR"
WHEEL_PATH=$(find "$ARTIFACT_DIR" -maxdepth 1 -type f -name '*.whl' -print -quit)
SDIST_PATH=$(find "$ARTIFACT_DIR" -maxdepth 1 -type f -name '*.tar.gz' -print -quit)
[[ -n $WHEEL_PATH && -n $SDIST_PATH ]] || fail "Expected one wheel and one source archive."
sha256sum "$WHEEL_PATH" "$SDIST_PATH" | tee "$ARTIFACT_DIR/SHA256SUMS"

CURRENT_STEP="inspecting package contents"
"$BUILD_PYTHON" - "$WHEEL_PATH" "$SDIST_PATH" <<'PY'
from pathlib import Path
import sys
import tarfile
import zipfile

wheel = Path(sys.argv[1])
sdist = Path(sys.argv[2])
forbidden = (".env", "docs/plan.md", "docs/session.md", "__pycache__", ".pyc", ".pyo")

with zipfile.ZipFile(wheel) as archive:
    wheel_names = archive.namelist()
with tarfile.open(sdist, "r:gz") as archive:
    sdist_names = archive.getnames()

for label, names in (("wheel", wheel_names), ("sdist", sdist_names)):
    unsafe = [name for name in names if any(marker in name for marker in forbidden)]
    if unsafe:
        raise SystemExit(f"{label} contains forbidden entries: {unsafe}")

required = {"graphmind/parsing.py", "graphmind/parser_worker.py"}
wheel_suffixes = {name.split("graphmind/", 1)[-1] for name in wheel_names if "graphmind/" in name}
missing = [name for name in required if name.split("graphmind/", 1)[-1] not in wheel_suffixes]
if missing:
    raise SystemExit(f"wheel is missing P3 modules: {missing}")

print(f"archive inspection passed: wheel={len(wheel_names)} entries, sdist={len(sdist_names)} entries")
PY

CURRENT_STEP="installing and testing the built wheel"
python3 -m venv "$WHEEL_VENV"
WHEEL_PYTHON="$WHEEL_VENV/bin/python"
WHEEL_GRAPHMIND="$WHEEL_VENV/bin/graphmind"
"$WHEEL_PYTHON" -m pip install --upgrade pip
"$WHEEL_PYTHON" -m pip install "$WHEEL_PATH"
"$WHEEL_PYTHON" -m pip check
"$WHEEL_GRAPHMIND" --help >/dev/null
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH= \
    "$WHEEL_PYTHON" -B -m unittest discover -s tests -v

CURRENT_STEP="running real five-format Chroma and Neo4j integration"
GRAPHMIND_RUN_INTEGRATION=1 \
GRAPHMIND_TEST_DOTENV="$DOTENV_FILE" \
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH= \
    "$WHEEL_PYTHON" -B -m unittest discover \
        -s tests/integration -p 'test_real_stores.py' -v

CURRENT_STEP="preparing isolated CLI acceptance data"
cat >"$INPUT_DIR/A.txt" <<'EOF'
The blue lantern protocol controls Zephyr authorization values. The answer record is maintained in [[B.txt]].
EOF
cat >"$INPUT_DIR/B.txt" <<'EOF'
The requested authorization value is CERULEAN.
EOF
cat >"$CONFIG_FILE" <<EOF
[graphmind]
data_dir = "$RUNTIME_DIR"
allowed_import_roots = ["$INPUT_DIR"]
collection_id = "ec2-test"
collection_name = "graphmind_ec2_${GIT_SHORT}_${RUN_ID//[^A-Za-z0-9]/_}"
max_file_bytes = 26214400
chunk_size = 200
chunk_overlap = 20
embedding_dimension = 64
job_lease_seconds = 60
max_queued_jobs = 100
max_extracted_chars = 10000000
max_archive_entries = 2048
max_archive_uncompressed_bytes = 104857600
max_pdf_pages = 1000
parser_timeout_seconds = 30
csv_delimiter = "auto"
EOF

GM=("$WHEEL_GRAPHMIND" --config "$CONFIG_FILE" --dotenv "$DOTENV_FILE")
"${GM[@]}" init >"$RUN_ROOT/cli-init.json"
"${GM[@]}" doctor | tee "$RUN_ROOT/cli-doctor.json"
"${GM[@]}" import "$INPUT_DIR/A.txt" >"$RUN_ROOT/cli-import-a.json"
"${GM[@]}" import "$INPUT_DIR/B.txt" >"$RUN_ROOT/cli-import-b.json"
"${GM[@]}" reindex "$INPUT_DIR/A.txt" >"$RUN_ROOT/cli-reindex-a.json"
"${GM[@]}" query --extractive \
    "Under the blue lantern protocol for Zephyr, what is the authorization value?" \
    | tee "$RUN_ROOT/cli-query.json"
"${GM[@]}" status >"$RUN_ROOT/cli-status-before-delete.json"

CURRENT_STEP="validating CLI answer and cleaning its store namespace"
"$WHEEL_PYTHON" - "$RUN_ROOT/cli-query.json" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("outcome") != "answer":
    raise SystemExit(f"CLI query did not answer: {payload}")
if not payload.get("citations"):
    raise SystemExit(f"CLI query returned no citation: {payload}")
print("CLI cited-answer validation passed")
PY

mapfile -t DOCUMENT_IDS < <("$WHEEL_PYTHON" - "$RUN_ROOT/cli-status-before-delete.json" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for document in payload.get("documents", []):
    print(document["document_id"])
PY
)
[[ ${#DOCUMENT_IDS[@]} -eq 2 ]] || fail "Expected two CLI test documents."
for document_id in "${DOCUMENT_IDS[@]}"; do
    "${GM[@]}" delete "$document_id" >/dev/null
done
"${GM[@]}" status >"$RUN_ROOT/cli-status-after-delete.json"
"$WHEEL_PYTHON" - "$RUN_ROOT/cli-status-after-delete.json" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
documents = payload.get("documents", [])
if not documents or any(not item.get("deleted") for item in documents):
    raise SystemExit(f"CLI cleanup did not tombstone every test document: {payload}")
if any(job.get("status") == "failed" for job in payload.get("jobs", [])):
    raise SystemExit(f"A CLI job failed: {payload}")
print("CLI delete/status validation passed")
PY
"${GM[@]}" doctor >"$RUN_ROOT/cli-doctor-after-delete.json"

if ((WITH_HOSTED_MODEL == 1)); then
    CURRENT_STEP="running hosted answer-model contract"
    if ! grep -Eq '^OLLAMA_API_KEY="?[^"[:space:]][^"[:space:]]*"?$' "$DOTENV_FILE"; then
        fail "Add OLLAMA_API_KEY to $DOTENV_FILE, then rerun with --with-hosted-model."
    fi
    GRAPHMIND_RUN_MODEL_INTEGRATION=1 \
    GRAPHMIND_TEST_DOTENV="$DOTENV_FILE" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH= \
        "$WHEEL_PYTHON" -B -m unittest discover \
            -s tests/integration -p 'test_real_stores.py' -v
fi

CURRENT_STEP="completion"
[[ -z $(git -C "$REPO_ROOT" status --porcelain --untracked-files=all) ]] || \
    fail "The test harness unexpectedly changed the original Git checkout."
printf '\nPASS: GraphMind P1-P3 Ubuntu test deployment completed.\n'
printf 'Commit: %s\n' "$GIT_HASH"
printf 'Instance type: %s (experimental; not a final capacity claim)\n' "$INSTANCE_TYPE"
printf 'Artifacts and logs: %s\n' "$RUN_ROOT"
printf 'Private test settings: %s\n' "$DOTENV_FILE"
printf 'Neo4j remains bound to localhost in container %s.\n' "$NEO4J_CONTAINER"
printf 'Google/OIDC, browser, MCP authorization, local inference, public HTTPS, load, and backup/restore were not tested.\n'

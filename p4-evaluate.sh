#!/usr/bin/env bash
# Run the frozen P4 quality and graph-scope experiments on Ubuntu 24.04.
# This is an isolated evaluation harness, not a public service deployment.

set -Eeuo pipefail
IFS=$'\n\t'

OLLAMA_VERSION_EXPECTED="0.34.0"
EMBEDDING_MODEL="bge-m3"
ANSWER_MODEL="gemma4:e2b"
LOCAL_ANSWER_TIMEOUT_SECONDS="${GRAPHMIND_P4_LOCAL_ANSWER_TIMEOUT_SECONDS:-180}"
LOCAL_ANSWER_MAX_RETRIES="${GRAPHMIND_P4_LOCAL_ANSWER_MAX_RETRIES:-0}"
INSTALL_OLLAMA=0
PULL_MODELS=0
WITH_HOSTED=0
HOSTED_ONLY=0
CURRENT_STEP="startup"

usage() {
    cat <<'EOF'
Usage: bash p4-evaluate.sh [options]

Options:
  --install-ollama  Install the pinned Ollama 0.34.0 Linux runtime using the
                    official installer, then verify the installed version.
  --pull-models     Pull bge-m3 and gemma4:e2b before evaluation.
  --with-hosted     Also run the hosted answer baseline configured in the
                    private dotenv file.
  --hosted-only     Run only the hosted-answer baseline. This still uses the
                    local bge-m3 embedding model, but skips local gemma4:e2b.
  -h, --help        Show this help.

Prerequisites:
  1. Run test-deploy.sh successfully so Neo4j and its private dotenv exist.
  2. Commit and pull the P4 candidate; this script only tests a clean commit.
  3. For --with-hosted, set OLLAMA_BASE_URL, GRAPHMIND_ANSWER_MODEL, and
     OLLAMA_API_KEY in the private dotenv reported by test-deploy.sh.

The local runs use only http://127.0.0.1:11434. Quality-gate failures are
retained as reports and do not prevent the remaining controlled experiments.
CPU inference defaults to a 180-second answer timeout with no retry so a slow
request is measured once. Override these evaluation-only values with
GRAPHMIND_P4_LOCAL_ANSWER_TIMEOUT_SECONDS and
GRAPHMIND_P4_LOCAL_ANSWER_MAX_RETRIES.
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
        --install-ollama) INSTALL_OLLAMA=1 ;;
        --pull-models) PULL_MODELS=1 ;;
        --with-hosted) WITH_HOSTED=1 ;;
        --hosted-only) WITH_HOSTED=1; HOSTED_ONLY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) fail "Unknown option: $1" ;;
    esac
    shift
done

[[ $LOCAL_ANSWER_TIMEOUT_SECONDS =~ ^[0-9]+$ ]] &&
    ((LOCAL_ANSWER_TIMEOUT_SECONDS >= 1)) ||
    fail "GRAPHMIND_P4_LOCAL_ANSWER_TIMEOUT_SECONDS must be a positive integer."
[[ $LOCAL_ANSWER_MAX_RETRIES =~ ^[0-9]+$ ]] &&
    ((LOCAL_ANSWER_MAX_RETRIES <= 5)) ||
    fail "GRAPHMIND_P4_LOCAL_ANSWER_MAX_RETRIES must be an integer from 0 through 5."

[[ ${EUID:-$(id -u)} -ne 0 ]] || fail "Run as the normal SSH user, not root."
[[ -r /etc/os-release ]] || fail "Cannot identify the operating system."
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == "ubuntu" && ${VERSION_ID:-} == "24.04" ]] || \
    fail "This P4 target requires Ubuntu 24.04 LTS; found ${PRETTY_NAME:-unknown}."

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null) || \
    fail "p4-evaluate.sh must be run from a Git checkout."
[[ $SCRIPT_DIR == "$REPO_ROOT" ]] || fail "Keep p4-evaluate.sh at the repository root."
cd "$REPO_ROOT"

[[ -f src/graphmind/evaluation.py && -f tests/test_graphmind_p4.py ]] || \
    fail "This checkout does not contain P4. Commit and pull the P4 candidate first."
[[ -f tests/fixtures/evaluation/metakgp_50.json ]] || fail "Frozen fixture is missing."
[[ -f Crawler/cleaned_wiki.jsonl ]] || fail "Frozen MetaKGP source snapshot is missing."
[[ -z $(git status --porcelain --untracked-files=all) ]] || \
    fail "The checkout is dirty. Test an exact committed candidate from a clean checkout."

GIT_HASH=$(git rev-parse HEAD)
GIT_SHORT=$(git rev-parse --short=12 HEAD)
STATE_ROOT="${GRAPHMIND_P4_STATE_DIR:-$HOME/.local/state/graphmind-p4-evaluation}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-${GIT_SHORT}-$$"
RUN_ROOT="$STATE_ROOT/runs/$RUN_ID"
CANDIDATE_ROOT="$RUN_ROOT/candidate"
VENV="$RUN_ROOT/venv"
REPORTS="$RUN_ROOT/reports"
LOG_FILE="$RUN_ROOT/p4-evaluate.log"
ENVIRONMENT_REPORT="$RUN_ROOT/environment.txt"
DOTENV_FILE="${GRAPHMIND_P4_DOTENV:-$HOME/.local/state/graphmind-test-deploy/secrets/graphmind-test.env}"

umask 077
mkdir -p "$RUN_ROOT" "$CANDIDATE_ROOT" "$REPORTS"
chmod 700 "$STATE_ROOT" "$RUN_ROOT"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

printf 'GraphMind P4 Ubuntu evaluation\n'
printf 'Commit: %s\n' "$GIT_HASH"
printf 'Run directory: %s\n' "$RUN_ROOT"
printf 'Log: %s\n' "$LOG_FILE"

for command_name in curl git python3 tar; do
    command -v "$command_name" >/dev/null || fail "Required command is missing: $command_name"
done
[[ -f $DOTENV_FILE ]] || \
    fail "Private Neo4j dotenv not found at $DOTENV_FILE. Run test-deploy.sh first or set GRAPHMIND_P4_DOTENV."
if ! (echo >/dev/tcp/127.0.0.1/7687) >/dev/null 2>&1; then
    fail "Neo4j is not listening on 127.0.0.1:7687. Start the test-deploy.sh container first."
fi

CURRENT_STEP="exporting the exact committed candidate"
git archive "$GIT_HASH" | tar -xf - -C "$CANDIDATE_ROOT"

if ((INSTALL_OLLAMA == 1)); then
    CURRENT_STEP="installing pinned Ollama ${OLLAMA_VERSION_EXPECTED}"
    INSTALL_SCRIPT="$RUN_ROOT/ollama-install.sh"
    curl -fsSL https://ollama.com/install.sh -o "$INSTALL_SCRIPT"
    chmod 600 "$INSTALL_SCRIPT"
    sha256sum "$INSTALL_SCRIPT" >"$RUN_ROOT/ollama-install.sha256"
    OLLAMA_VERSION="$OLLAMA_VERSION_EXPECTED" sh "$INSTALL_SCRIPT"
fi

command -v ollama >/dev/null || \
    fail "Ollama is missing. Rerun with --install-ollama or install version $OLLAMA_VERSION_EXPECTED."
OLLAMA_CLI_VERSION=$(ollama --version 2>&1)
if [[ $OLLAMA_CLI_VERSION != *"$OLLAMA_VERSION_EXPECTED"* ]]; then
    fail "Expected Ollama $OLLAMA_VERSION_EXPECTED; found: $OLLAMA_CLI_VERSION"
fi
if command -v systemctl >/dev/null; then
    sudo systemctl enable --now ollama
fi

OLLAMA_READY=0
for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
        OLLAMA_READY=1
        break
    fi
    sleep 2
done
((OLLAMA_READY == 1)) || fail "Ollama did not become ready on 127.0.0.1:11434."

if ((PULL_MODELS == 1)); then
    CURRENT_STEP="pulling exact P4 model tags"
    ollama pull "$EMBEDDING_MODEL"
    if ((HOSTED_ONLY == 0)); then
        ollama pull "$ANSWER_MODEL"
    fi
fi

CURRENT_STEP="verifying local model availability"
TAGS_FILE="$RUN_ROOT/ollama-tags.json"
curl -fsS http://127.0.0.1:11434/api/tags -o "$TAGS_FILE"
REQUIRED_MODELS=("$EMBEDDING_MODEL")
if ((HOSTED_ONLY == 0)); then
    REQUIRED_MODELS+=("$ANSWER_MODEL")
fi
python3 - "$TAGS_FILE" "${REQUIRED_MODELS[@]}" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
names = {
    str(model.get("name", ""))
    for model in payload.get("models", [])
    if isinstance(model, dict)
}
missing = [name for name in sys.argv[2:] if name not in names and f"{name}:latest" not in names]
if missing:
    raise SystemExit(f"Missing local model tags: {', '.join(missing)}. Rerun with --pull-models.")
for model in payload.get("models", []):
    if isinstance(model, dict) and any(
        model.get("name") in {name, f"{name}:latest"} for name in sys.argv[2:]
    ):
        print(
            json.dumps(
                {
                    "name": model.get("name"),
                    "digest": model.get("digest"),
                    "size": model.get("size"),
                    "details": model.get("details"),
                },
                sort_keys=True,
            )
        )
PY

CURRENT_STEP="recording host and runtime configuration"
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
    printf 'ollama_cli=%s\n' "$OLLAMA_CLI_VERSION"
    printf 'ollama_server=%s\n' "$(curl -fsS http://127.0.0.1:11434/api/version)"
    printf 'local_answer_timeout_seconds=%s\n' "$LOCAL_ANSWER_TIMEOUT_SECONDS"
    printf 'local_answer_max_retries=%s\n' "$LOCAL_ANSWER_MAX_RETRIES"
    printf '\nCPU\n'; lscpu
    printf '\nMEMORY\n'; free -h
    printf '\nFILESYSTEM\n'; df -hT "$HOME"
    printf '\nGPU\n'
    if command -v nvidia-smi >/dev/null; then nvidia-smi; else printf 'none detected\n'; fi
} >"$ENVIRONMENT_REPORT" 2>&1
cat "$ENVIRONMENT_REPORT"
if [[ $INSTANCE_TYPE == "t3.large" ]]; then
    printf 'WARNING: t3.large has no GPU and is an experimental sizing probe.\n'
fi

CURRENT_STEP="installing the exact P4 candidate"
python3 -m venv "$VENV"
PYTHON="$VENV/bin/python"
GRAPHMIND="$VENV/bin/graphmind"
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install "$CANDIDATE_ROOT"
"$PYTHON" -m pip check

CURRENT_STEP="running offline regression tests"
cd "$CANDIDATE_ROOT"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH= \
    "$PYTHON" -B -m unittest discover -s tests -v

if ((HOSTED_ONLY == 0)); then
    CURRENT_STEP="running real local model contract"
    GRAPHMIND_RUN_LOCAL_MODEL_INTEGRATION=1 \
    GRAPHMIND_LOCAL_OLLAMA_BASE_URL="http://127.0.0.1:11434" \
    GRAPHMIND_LOCAL_EMBEDDING_MODEL="$EMBEDDING_MODEL" \
    GRAPHMIND_LOCAL_ANSWER_MODEL="$ANSWER_MODEL" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH= \
        "$PYTHON" -B -m unittest discover \
            -s tests/integration -p 'test_real_stores.py' -v
fi

RESULTS_FILE="$RUN_ROOT/results.tsv"
touch "$RESULTS_FILE"

run_evaluation() {
    local label=$1
    local graph_scope=$2
    local vector_only=$3
    local answer_mode=$4
    local work_dir="$RUN_ROOT/work-$label"
    local report="$REPORTS/$label.json"
    local collection="gm_p4_${GIT_SHORT}_${label//[^A-Za-z0-9]/_}_$$"
    local extra_args=()
    local answer_env=()
    if [[ $vector_only == "yes" ]]; then extra_args+=(--vector-only); fi
    if [[ $answer_mode == "local" ]]; then
        answer_env+=(
            GRAPHMIND_ANSWER_MODE=local
            GRAPHMIND_ANSWER_PROVIDER=ollama
            GRAPHMIND_ANSWER_MODEL="$ANSWER_MODEL"
            GRAPHMIND_ANSWER_TIMEOUT_SECONDS="$LOCAL_ANSWER_TIMEOUT_SECONDS"
            GRAPHMIND_ANSWER_MAX_RETRIES="$LOCAL_ANSWER_MAX_RETRIES"
            OLLAMA_BASE_URL=http://127.0.0.1:11434
            OLLAMA_API_KEY=
        )
    else
        answer_env+=(GRAPHMIND_ANSWER_MODE=hosted)
    fi

    CURRENT_STEP="evaluating $label"
    set +e
    env \
        GRAPHMIND_COLLECTION_NAME="$collection" \
        GRAPHMIND_EMBEDDING_PROVIDER=ollama \
        GRAPHMIND_EMBEDDING_MODEL="$EMBEDDING_MODEL" \
        GRAPHMIND_EMBEDDING_MODEL_ID=BAAI/bge-m3 \
        GRAPHMIND_EMBEDDING_REVISION=auto \
        GRAPHMIND_EMBEDDING_PRECISION=auto \
        GRAPHMIND_EMBEDDING_BASE_URL=http://127.0.0.1:11434 \
        GRAPHMIND_EMBEDDING_DIMENSION=1024 \
        GRAPHMIND_RETRIEVAL_GRAPH_SCOPE="$graph_scope" \
        "${answer_env[@]}" \
        "$GRAPHMIND" --dotenv "$DOTENV_FILE" evaluate \
            --fixture "$CANDIDATE_ROOT/tests/fixtures/evaluation/metakgp_50.json" \
            --source "$CANDIDATE_ROOT/Crawler/cleaned_wiki.jsonl" \
            --work-dir "$work_dir" \
            --report "$report" \
            --label "$label" \
            --graph-scope "$graph_scope" \
            --resolve-model-identity \
            "${extra_args[@]}"
    local status=$?
    set -e
    if [[ $status -ne 0 && $status -ne 4 ]]; then
        fail "$label failed before producing a quality result (exit $status)."
    fi
    [[ -s $report ]] || fail "$label did not write its report."
    printf '%s\t%s\t%s\n' "$label" "$status" "$report" >>"$RESULTS_FILE"
}

# P4.6 accepted baseline, followed by the P4.7 controlled comparisons.
if ((HOSTED_ONLY == 0)); then
    run_evaluation "local-chunk" "chunk" "no" "local"
    run_evaluation "local-vector-only" "chunk" "yes" "local"
    run_evaluation "local-document" "document" "no" "local"
fi

if ((WITH_HOSTED == 1)); then
    CURRENT_STEP="validating hosted provider configuration"
    env GRAPHMIND_ANSWER_MODE=hosted \
        "$PYTHON" - "$DOTENV_FILE" <<'PY'
from pathlib import Path
import sys
from graphmind.config import Settings

settings = Settings.load(dotenv_path=Path(sys.argv[1]), environ={"GRAPHMIND_ANSWER_MODE": "hosted"})
settings.validate(require_provider_secret=True)
print("Hosted provider configuration is present; secret value was not printed.")
PY
    run_evaluation "hosted-chunk" "chunk" "no" "hosted"
fi

CURRENT_STEP="summarizing P4 evidence"
set +e
"$PYTHON" - "$RESULTS_FILE" "$WITH_HOSTED" "$HOSTED_ONLY" <<'PY'
import json
from pathlib import Path
import sys

rows = []
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    label, status, report_path = line.split("\t")
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    summary = report["summary"]
    status = int(status)
    expected_status = 0 if bool(summary["gate_passed"]) else 4
    if status != expected_status:
        raise SystemExit(
            f"{label} exit/report mismatch: exit={status}, "
            f"gate_passed={summary['gate_passed']}"
        )
    rows.append((label, status, summary))
    print(
        f"{label}: gate={summary['gate_passed']} "
        f"answers={summary['answer_passes']}/{summary['total_cases']} "
        f"retrieval={summary['retrieval_passes']}/{summary['retrieval_case_count']} "
        f"graph_target_recall={summary['graph_target_recall']:.3f} "
        f"p95_ms={summary['p95_duration_ms']:.1f}"
    )

by_label = {label: summary for label, _, summary in rows}
quality_failed = False
if sys.argv[3] == "1":
    if bool(by_label["hosted-chunk"]["gate_passed"]):
        print("Hosted automated threshold passed; combine with the prior local report and perform manual factual-support review.")
    else:
        print("P4 gate is not passed: the hosted chunk-scope baseline missed its threshold.")
        quality_failed = True
elif not bool(by_label["local-chunk"]["gate_passed"]):
    print("P4 gate is not passed: the accepted local chunk-scope baseline missed its threshold.")
    quality_failed = True
elif sys.argv[2] != "1":
    print("Local baseline passed; P4 remains incomplete until the hosted baseline is run.")
elif not bool(by_label["hosted-chunk"]["gate_passed"]):
    print("P4 gate is not passed: the hosted chunk-scope baseline missed its threshold.")
    quality_failed = True
else:
    print("Automated local and hosted thresholds passed. Manual factual-support review is still required.")
raise SystemExit(4 if quality_failed else 0)
PY
SUMMARY_STATUS=$?
set -e
if [[ $SUMMARY_STATUS -ne 0 && $SUMMARY_STATUS -ne 4 ]]; then
    fail "Could not summarize the P4 quality reports (exit $SUMMARY_STATUS)."
fi

[[ -z $(git -C "$REPO_ROOT" status --porcelain --untracked-files=all) ]] || \
    fail "The P4 harness unexpectedly changed the original Git checkout."
printf '\nP4 evaluation completed.\n'
printf 'Commit: %s\n' "$GIT_HASH"
printf 'Reports and environment evidence: %s\n' "$RUN_ROOT"
printf 'No public service, OAuth, MCP endpoint, or AWS security group was changed.\n'
exit "$SUMMARY_STATUS"

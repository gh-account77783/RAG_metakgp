"""Validated, side-effect-free configuration loading."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import ConfigurationError


_ENV_KEYS = {
    "GRAPHMIND_DATA_DIR": "data_dir",
    "GRAPHMIND_ALLOWED_IMPORT_ROOTS": "allowed_import_roots",
    "GRAPHMIND_COLLECTION_ID": "collection_id",
    "GRAPHMIND_COLLECTION_NAME": "collection_name",
    "GRAPHMIND_MAX_FILE_BYTES": "max_file_bytes",
    "GRAPHMIND_CHUNK_SIZE": "chunk_size",
    "GRAPHMIND_CHUNK_OVERLAP": "chunk_overlap",
    "GRAPHMIND_EMBEDDING_PROVIDER": "embedding_provider",
    "GRAPHMIND_EMBEDDING_MODEL": "embedding_model",
    "GRAPHMIND_EMBEDDING_MODEL_ID": "embedding_model_id",
    "GRAPHMIND_EMBEDDING_REVISION": "embedding_revision",
    "GRAPHMIND_EMBEDDING_PRECISION": "embedding_precision",
    "GRAPHMIND_EMBEDDING_BASE_URL": "embedding_base_url",
    "GRAPHMIND_EMBEDDING_API_KEY": "embedding_api_key",
    "GRAPHMIND_EMBEDDING_DIMENSION": "embedding_dimension",
    "GRAPHMIND_EMBEDDING_TIMEOUT_SECONDS": "embedding_timeout_seconds",
    "GRAPHMIND_EMBEDDING_BATCH_SIZE": "embedding_batch_size",
    "GRAPHMIND_JOB_LEASE_SECONDS": "job_lease_seconds",
    "GRAPHMIND_MAX_QUEUED_JOBS": "max_queued_jobs",
    "GRAPHMIND_MAX_EXTRACTED_CHARS": "max_extracted_chars",
    "GRAPHMIND_MAX_ARCHIVE_ENTRIES": "max_archive_entries",
    "GRAPHMIND_MAX_ARCHIVE_UNCOMPRESSED_BYTES": "max_archive_uncompressed_bytes",
    "GRAPHMIND_MAX_PDF_PAGES": "max_pdf_pages",
    "GRAPHMIND_PARSER_TIMEOUT_SECONDS": "parser_timeout_seconds",
    "GRAPHMIND_CSV_DELIMITER": "csv_delimiter",
    "NEO4J_URI": "neo4j_uri",
    "NEO4J_USERNAME": "neo4j_username",
    "NEO4J_PASSWORD": "neo4j_password",
    "NEO4J_DATABASE": "neo4j_database",
    "OLLAMA_BASE_URL": "ollama_base_url",
    "OLLAMA_API_KEY": "ollama_api_key",
    "ollama_api_key": "ollama_api_key",
    "GRAPHMIND_ANSWER_MODEL": "answer_model",
    "GRAPHMIND_ANSWER_PROVIDER": "answer_provider",
    "GRAPHMIND_ANSWER_MODE": "answer_mode",
    "GRAPHMIND_ANSWER_TIMEOUT_SECONDS": "answer_timeout_seconds",
    "GRAPHMIND_ANSWER_MAX_RETRIES": "answer_max_retries",
    "GRAPHMIND_ANSWER_MAX_CONTEXT_CHARS": "answer_max_context_chars",
    "GRAPHMIND_ANSWER_MAX_OUTPUT_TOKENS": "answer_max_output_tokens",
    "GRAPHMIND_RETRIEVAL_SEED_LIMIT": "retrieval_seed_limit",
    "GRAPHMIND_RETRIEVAL_GRAPH_LIMIT": "retrieval_graph_limit",
    "GRAPHMIND_RETRIEVAL_GRAPH_SCOPE": "retrieval_graph_scope",
    "GRAPHMIND_RETRIEVAL_MAX_EVIDENCE": "retrieval_max_evidence",
    "GRAPHMIND_RETRIEVAL_MIN_VECTOR_SCORE": "retrieval_min_vector_score",
    "GRAPHMIND_MAX_QUESTION_CHARS": "max_question_chars",
    "GRAPHMIND_MAX_CONCURRENT_QUERIES": "max_concurrent_queries",
    "GRAPHMIND_MAX_QUEUED_QUERIES": "max_queued_queries",
    "GRAPHMIND_QUERY_QUEUE_TIMEOUT_SECONDS": "query_queue_timeout_seconds",
}


def _parse_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    parsed: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ConfigurationError(f"Cannot read environment file: {path}") from exc
    for line_number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not match:
            raise ConfigurationError(f"Invalid environment line {line_number} in {path}")
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        parsed[match.group(1)] = value
    return parsed


def _parse_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as source:
            payload = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Cannot parse configuration file: {path}") from exc
    section = payload.get("graphmind", payload)
    if not isinstance(section, dict):
        raise ConfigurationError("The graphmind configuration section must be a table")
    return dict(section)


def _coerce_path(value: object, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def _coerce_roots(value: object, base: Path) -> tuple[Path, ...]:
    if isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        parts = [item for item in str(value).split(os.pathsep) if item]
    if not parts:
        raise ConfigurationError("At least one allowed import root is required")
    return tuple(_coerce_path(item, base) for item in parts)


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    allowed_import_roots: tuple[Path, ...]
    collection_id: str = "default"
    collection_name: str = "graphmind_chunks"
    max_file_bytes: int = 25 * 1024 * 1024
    chunk_size: int = 1200
    chunk_overlap: int = 120
    embedding_provider: str = "hash"
    embedding_model: str = "bge-m3"
    embedding_model_id: str = "BAAI/bge-m3"
    embedding_revision: str = "auto"
    embedding_precision: str = "auto"
    embedding_base_url: str = "http://127.0.0.1:11434"
    embedding_api_key: str = field(default="", repr=False)
    embedding_dimension: int = 384
    embedding_timeout_seconds: int = 60
    embedding_batch_size: int = 16
    job_lease_seconds: int = 60
    max_queued_jobs: int = 100
    max_extracted_chars: int = 10_000_000
    max_archive_entries: int = 2048
    max_archive_uncompressed_bytes: int = 100 * 1024 * 1024
    max_pdf_pages: int = 1000
    parser_timeout_seconds: int = 30
    csv_delimiter: str = "auto"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = field(default="", repr=False)
    neo4j_database: str = "neo4j"
    ollama_base_url: str = "https://ollama.com"
    ollama_api_key: str = field(default="", repr=False)
    answer_model: str = "gemma4:31b-cloud"
    answer_provider: str = "ollama"
    answer_mode: str = "hosted"
    answer_timeout_seconds: int = 60
    answer_max_retries: int = 2
    answer_max_context_chars: int = 24_000
    answer_max_output_tokens: int = 512
    retrieval_seed_limit: int = 4
    retrieval_graph_limit: int = 8
    retrieval_graph_scope: str = "chunk"
    retrieval_max_evidence: int = 12
    retrieval_min_vector_score: float = 0.02
    max_question_chars: int = 2000
    max_concurrent_queries: int = 4
    max_queued_queries: int = 8
    query_queue_timeout_seconds: float = 1.0

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "metadata.sqlite3"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def chroma_dir(self) -> Path:
        return self.data_dir / "chroma"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @classmethod
    def load(
        cls,
        *,
        config_path: Path | None = None,
        dotenv_path: Path | None = None,
        environ: Mapping[str, str] | None = None,
        explicit: Mapping[str, object] | None = None,
    ) -> "Settings":
        env = dict(os.environ if environ is None else environ)
        cwd = Path.cwd().resolve()
        selected_config = config_path
        if selected_config is None and env.get("GRAPHMIND_CONFIG_FILE"):
            selected_config = Path(env["GRAPHMIND_CONFIG_FILE"])
        if selected_config is None:
            candidate = cwd / "graphmind.toml"
            selected_config = candidate if candidate.is_file() else None
        selected_dotenv = dotenv_path
        if selected_dotenv is None:
            candidate = cwd / ".env"
            selected_dotenv = candidate if candidate.is_file() else None

        values: dict[str, object] = {
            "data_dir": cwd / ".graphmind-data",
            "allowed_import_roots": (cwd,),
            "collection_id": "default",
            "collection_name": "graphmind_chunks",
            "max_file_bytes": 25 * 1024 * 1024,
            "chunk_size": 1200,
            "chunk_overlap": 120,
            "embedding_provider": "hash",
            "embedding_model": "bge-m3",
            "embedding_model_id": "BAAI/bge-m3",
            "embedding_revision": "auto",
            "embedding_precision": "auto",
            "embedding_base_url": "http://127.0.0.1:11434",
            "embedding_api_key": "",
            "embedding_dimension": 384,
            "embedding_timeout_seconds": 60,
            "embedding_batch_size": 16,
            "job_lease_seconds": 60,
            "max_queued_jobs": 100,
            "max_extracted_chars": 10_000_000,
            "max_archive_entries": 2048,
            "max_archive_uncompressed_bytes": 100 * 1024 * 1024,
            "max_pdf_pages": 1000,
            "parser_timeout_seconds": 30,
            "csv_delimiter": "auto",
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_username": "neo4j",
            "neo4j_password": "",
            "neo4j_database": "neo4j",
            "ollama_base_url": "https://ollama.com",
            "ollama_api_key": "",
            "answer_model": "gemma4:31b-cloud",
            "answer_provider": "ollama",
            "answer_mode": "hosted",
            "answer_timeout_seconds": 60,
            "answer_max_retries": 2,
            "answer_max_context_chars": 24_000,
            "answer_max_output_tokens": 512,
            "retrieval_seed_limit": 4,
            "retrieval_graph_limit": 8,
            "retrieval_graph_scope": "chunk",
            "retrieval_max_evidence": 12,
            "retrieval_min_vector_score": 0.02,
            "max_question_chars": 2000,
            "max_concurrent_queries": 4,
            "max_queued_queries": 8,
            "query_queue_timeout_seconds": 1.0,
        }
        if selected_config is not None:
            path = selected_config.expanduser().resolve()
            values.update(_parse_toml(path))
            base = path.parent
        else:
            base = cwd
        dotenv_values = _parse_dotenv(selected_dotenv.expanduser().resolve()) if selected_dotenv else {}
        for source in (dotenv_values, env):
            for env_key, field_name in _ENV_KEYS.items():
                if env_key in source:
                    values[field_name] = source[env_key]
        if explicit:
            values.update(explicit)

        values["data_dir"] = _coerce_path(values["data_dir"], base)
        values["allowed_import_roots"] = _coerce_roots(values["allowed_import_roots"], base)
        for key in (
            "max_file_bytes",
            "chunk_size",
            "chunk_overlap",
            "embedding_dimension",
            "embedding_timeout_seconds",
            "embedding_batch_size",
            "job_lease_seconds",
            "max_queued_jobs",
            "max_extracted_chars",
            "max_archive_entries",
            "max_archive_uncompressed_bytes",
            "max_pdf_pages",
            "parser_timeout_seconds",
            "answer_timeout_seconds",
            "answer_max_retries",
            "answer_max_context_chars",
            "answer_max_output_tokens",
            "retrieval_seed_limit",
            "retrieval_graph_limit",
            "retrieval_max_evidence",
            "max_question_chars",
            "max_concurrent_queries",
            "max_queued_queries",
        ):
            try:
                values[key] = int(values[key])
            except (TypeError, ValueError) as exc:
                raise ConfigurationError(f"{key} must be an integer") from exc
        for key in ("retrieval_min_vector_score", "query_queue_timeout_seconds"):
            try:
                values[key] = float(values[key])
            except (TypeError, ValueError) as exc:
                raise ConfigurationError(f"{key} must be a number") from exc
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ConfigurationError(f"Unknown configuration fields: {', '.join(unknown)}")
        settings = cls(**values)  # type: ignore[arg-type]
        settings.validate()
        return settings

    def validate(self, *, require_neo4j_secret: bool = False, require_provider_secret: bool = False) -> None:
        if not self.collection_id.strip() or not self.collection_name.strip():
            raise ConfigurationError("Collection identifiers cannot be empty")
        if self.max_file_bytes <= 0:
            raise ConfigurationError("max_file_bytes must be greater than zero")
        if self.chunk_size <= 0 or self.chunk_overlap < 0 or self.chunk_overlap >= self.chunk_size:
            raise ConfigurationError("chunk overlap must be non-negative and smaller than chunk size")
        if self.embedding_dimension < 32:
            raise ConfigurationError("embedding_dimension must be at least 32")
        if self.embedding_provider not in {"hash", "ollama"}:
            raise ConfigurationError("GRAPHMIND_EMBEDDING_PROVIDER must be 'hash' or 'ollama'")
        if not self.embedding_model.strip() or not self.embedding_model_id.strip():
            raise ConfigurationError("Embedding model identifiers cannot be empty")
        if self.embedding_provider == "ollama" and (
            self.embedding_model_id.casefold() == "baai/bge-m3"
            and self.embedding_dimension != 1024
        ):
            raise ConfigurationError("BAAI/bge-m3 requires embedding_dimension=1024")
        if self.embedding_revision != "auto" and not re.fullmatch(
            r"[0-9a-fA-F]{64}", self.embedding_revision
        ):
            raise ConfigurationError("embedding_revision must be 'auto' or a full SHA-256 digest")
        if self.embedding_revision != "auto" and self.embedding_precision == "auto":
            raise ConfigurationError(
                "embedding_precision must be explicit when embedding_revision is pinned"
            )
        if self.embedding_precision.casefold() not in {
            "auto",
            "fp8",
            "f8",
            "fp16",
            "f16",
            "int8",
            "q8_0",
        }:
            raise ConfigurationError("embedding_precision must be auto, FP8, FP16, or INT8")
        if self.embedding_timeout_seconds < 1 or self.embedding_batch_size < 1:
            raise ConfigurationError("Embedding timeout and batch size must be positive")
        if self.job_lease_seconds < 5:
            raise ConfigurationError("job_lease_seconds must be at least 5")
        if self.max_queued_jobs < 1:
            raise ConfigurationError("max_queued_jobs must be at least 1")
        if self.max_extracted_chars < 1:
            raise ConfigurationError("max_extracted_chars must be at least 1")
        if self.max_archive_entries < 1:
            raise ConfigurationError("max_archive_entries must be at least 1")
        if self.max_archive_uncompressed_bytes < 1:
            raise ConfigurationError("max_archive_uncompressed_bytes must be at least 1")
        if self.max_pdf_pages < 1:
            raise ConfigurationError("max_pdf_pages must be at least 1")
        if self.parser_timeout_seconds < 1:
            raise ConfigurationError("parser_timeout_seconds must be at least 1")
        if self.csv_delimiter not in {"auto", "comma", "semicolon", "tab", "pipe"}:
            raise ConfigurationError(
                "csv_delimiter must be auto, comma, semicolon, tab, or pipe"
            )
        scheme = urlparse(self.neo4j_uri).scheme
        if scheme not in {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"}:
            raise ConfigurationError("NEO4J_URI must use a Neo4j or Bolt scheme")
        provider_url = urlparse(self.ollama_base_url)
        if provider_url.scheme not in {"http", "https"} or not provider_url.netloc:
            raise ConfigurationError("OLLAMA_BASE_URL must be an absolute HTTP(S) URL")
        if self.answer_provider != "ollama":
            raise ConfigurationError("GRAPHMIND_ANSWER_PROVIDER must be 'ollama'")
        if self.answer_mode not in {"local", "hosted"}:
            raise ConfigurationError("GRAPHMIND_ANSWER_MODE must be 'local' or 'hosted'")
        if self.answer_mode == "local" and provider_url.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ConfigurationError("Local answer mode requires a loopback OLLAMA_BASE_URL")
        if not self.answer_model.strip():
            raise ConfigurationError("GRAPHMIND_ANSWER_MODEL cannot be empty")
        embedding_url = urlparse(self.embedding_base_url)
        if embedding_url.scheme not in {"http", "https"} or not embedding_url.netloc:
            raise ConfigurationError("GRAPHMIND_EMBEDDING_BASE_URL must be an absolute HTTP(S) URL")
        if self.embedding_provider == "ollama" and embedding_url.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ConfigurationError("Ollama embeddings require a loopback embedding base URL")
        if self.answer_timeout_seconds < 1 or self.answer_max_retries not in range(0, 6):
            raise ConfigurationError("Answer timeout must be positive and retries must be between 0 and 5")
        if self.answer_max_context_chars < 1000 or self.answer_max_output_tokens < 1:
            raise ConfigurationError("Answer context and output budgets are invalid")
        if self.retrieval_seed_limit < 1 or self.retrieval_graph_limit < 0:
            raise ConfigurationError("Retrieval seed/graph limits are invalid")
        if self.retrieval_graph_scope not in {"chunk", "document"}:
            raise ConfigurationError("retrieval_graph_scope must be 'chunk' or 'document'")
        if self.retrieval_max_evidence < self.retrieval_seed_limit:
            raise ConfigurationError("retrieval_max_evidence must be at least retrieval_seed_limit")
        if not -1.0 <= self.retrieval_min_vector_score <= 1.0:
            raise ConfigurationError("retrieval_min_vector_score must be between -1 and 1")
        if self.max_question_chars < 1 or self.max_concurrent_queries < 1:
            raise ConfigurationError("Question and concurrency budgets must be positive")
        if self.max_queued_queries < 0:
            raise ConfigurationError("max_queued_queries cannot be negative")
        if self.query_queue_timeout_seconds < 0:
            raise ConfigurationError("query_queue_timeout_seconds cannot be negative")
        if require_neo4j_secret and not self.neo4j_password:
            raise ConfigurationError("NEO4J_PASSWORD is required for Neo4j operations")
        if (
            require_provider_secret
            and self.answer_provider == "ollama"
            and self.answer_mode == "hosted"
            and not self.ollama_api_key
        ):
            raise ConfigurationError("OLLAMA_API_KEY is required for the selected answer provider")

    def ensure_private_paths(self) -> None:
        for path in (
            self.data_dir,
            self.files_dir,
            self.staging_dir,
            self.chroma_dir,
            self.logs_dir,
            self.backups_dir,
        ):
            try:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                probe = path / ".graphmind-write-check"
                probe.write_bytes(b"")
                probe.unlink()
            except OSError as exc:
                raise ConfigurationError(f"Data path is not writable: {path}") from exc

    def with_data_dir(self, path: Path) -> "Settings":
        updated = replace(self, data_dir=path.expanduser().resolve())
        updated.validate()
        return updated

    def diagnostics(self) -> dict[str, object]:
        return {
            "data_dir": str(self.data_dir),
            "allowed_import_roots": [str(path) for path in self.allowed_import_roots],
            "collection_id": self.collection_id,
            "collection_name": self.collection_name,
            "max_file_bytes": self.max_file_bytes,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
            "embedding_model_id": self.embedding_model_id,
            "embedding_revision": self.embedding_revision,
            "embedding_precision": self.embedding_precision,
            "embedding_base_url": self.embedding_base_url,
            "embedding_api_key": "<configured>" if self.embedding_api_key else "<missing>",
            "embedding_dimension": self.embedding_dimension,
            "embedding_timeout_seconds": self.embedding_timeout_seconds,
            "embedding_batch_size": self.embedding_batch_size,
            "job_lease_seconds": self.job_lease_seconds,
            "max_queued_jobs": self.max_queued_jobs,
            "max_extracted_chars": self.max_extracted_chars,
            "max_archive_entries": self.max_archive_entries,
            "max_archive_uncompressed_bytes": self.max_archive_uncompressed_bytes,
            "max_pdf_pages": self.max_pdf_pages,
            "parser_timeout_seconds": self.parser_timeout_seconds,
            "csv_delimiter": self.csv_delimiter,
            "neo4j_uri": self.neo4j_uri,
            "neo4j_username": self.neo4j_username,
            "neo4j_password": "<configured>" if self.neo4j_password else "<missing>",
            "neo4j_database": self.neo4j_database,
            "answer_provider": self.answer_provider,
            "answer_model": self.answer_model,
            "answer_mode": self.answer_mode,
            "answer_timeout_seconds": self.answer_timeout_seconds,
            "answer_max_retries": self.answer_max_retries,
            "answer_max_context_chars": self.answer_max_context_chars,
            "answer_max_output_tokens": self.answer_max_output_tokens,
            "retrieval_seed_limit": self.retrieval_seed_limit,
            "retrieval_graph_limit": self.retrieval_graph_limit,
            "retrieval_graph_scope": self.retrieval_graph_scope,
            "retrieval_max_evidence": self.retrieval_max_evidence,
            "retrieval_min_vector_score": self.retrieval_min_vector_score,
            "max_question_chars": self.max_question_chars,
            "max_concurrent_queries": self.max_concurrent_queries,
            "max_queued_queries": self.max_queued_queries,
            "query_queue_timeout_seconds": self.query_queue_timeout_seconds,
            "ollama_base_url": self.ollama_base_url,
            "ollama_api_key": "<configured>" if self.ollama_api_key else "<missing>",
        }

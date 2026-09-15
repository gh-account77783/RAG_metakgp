"""Frozen MetaKGP materialization and deterministic P4 scoring."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .domain import Evidence, Outcome
from .errors import EvaluationError
from .retrieval import AnswerService


def _normalized_source_bytes(raw: bytes) -> bytes:
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _safe_filename(title: str, url: str) -> str:
    source = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or title
    slug = re.sub(r"[^A-Za-z0-9]+", "-", source).strip("-").casefold()[:60]
    if not slug:
        slug = "document"
    return f"{slug}-{_sha256(url.encode('utf-8'))[:10]}.md"


@dataclass(frozen=True, slots=True)
class MaterializedEvaluation:
    root: Path
    manifest_path: Path
    dataset_id: str
    materialization_sha256: str
    url_to_filename: dict[str, str]

    @property
    def filename_to_url(self) -> dict[str, str]:
        return {name: url for url, name in self.url_to_filename.items()}

    @property
    def document_paths(self) -> tuple[Path, ...]:
        return tuple(self.root / name for name in sorted(self.url_to_filename.values()))


class FrozenEvaluationDataset:
    def __init__(self, fixture_path: Path, source_path: Path) -> None:
        self.fixture_path = fixture_path.resolve()
        self.source_path = source_path.resolve()
        try:
            self.fixture = json.loads(self.fixture_path.read_text(encoding="utf-8"))
            source_bytes = self.source_path.read_bytes()
        except (OSError, json.JSONDecodeError) as exc:
            raise EvaluationError("Cannot read the frozen evaluation inputs") from exc
        if not isinstance(self.fixture, dict) or not isinstance(self.fixture.get("cases"), list):
            raise EvaluationError("Evaluation fixture has an invalid structure")
        source = self.fixture.get("source")
        if not isinstance(source, dict):
            raise EvaluationError("Evaluation fixture is missing source metadata")
        if source.get("sha256_normalization") != "UTF-8 bytes with CRLF and lone CR converted to LF":
            raise EvaluationError("Evaluation fixture uses an unsupported source-hash contract")
        actual_hash = _sha256(_normalized_source_bytes(source_bytes))
        if actual_hash != source.get("sha256"):
            raise EvaluationError("Evaluation source snapshot hash does not match the fixture")
        documents: list[dict[str, Any]] = []
        try:
            for line in source_bytes.decode("utf-8").splitlines():
                if line.strip():
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise TypeError
                    documents.append(item)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise EvaluationError("Evaluation source snapshot is invalid JSONL") from exc
        if len(documents) != int(source.get("document_count", -1)):
            raise EvaluationError("Evaluation source document count does not match the fixture")
        self.documents_by_url = {str(item.get("url", "")): item for item in documents}
        self.dataset_id = str(self.fixture.get("dataset_id", ""))
        if not self.dataset_id:
            raise EvaluationError("Evaluation fixture has no dataset ID")

    @property
    def cases(self) -> list[dict[str, Any]]:
        return list(self.fixture["cases"])

    def _selected_urls(self) -> set[str]:
        urls: set[str] = set()
        for case in self.cases:
            for evidence in case.get("evidence", []):
                urls.add(str(evidence["url"]))
            for edge in case.get("graph_edges", []):
                urls.add(str(edge["from_url"]))
                urls.add(str(edge["to_url"]))
        missing = urls - self.documents_by_url.keys()
        if missing:
            raise EvaluationError("Evaluation fixture references a missing source document")
        return urls

    def materialize(self, output_dir: Path) -> MaterializedEvaluation:
        root = output_dir.resolve()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise EvaluationError("Cannot create the evaluation materialization directory") from exc
        urls = self._selected_urls()
        url_to_filename = {
            url: _safe_filename(str(self.documents_by_url[url].get("title", "")), url)
            for url in sorted(urls)
        }
        expected_names = set(url_to_filename.values()) | {"manifest.json"}
        existing_names = {path.name for path in root.iterdir() if path.is_file()}
        unexpected = existing_names - expected_names
        if unexpected:
            raise EvaluationError(
                "Evaluation materialization directory contains unexpected files"
            )

        targets_by_source: dict[str, set[str]] = {}
        for case in self.cases:
            for edge in case.get("graph_edges", []):
                targets_by_source.setdefault(str(edge["from_url"]), set()).add(
                    str(edge["to_url"])
                )

        manifest_documents: list[dict[str, Any]] = []
        for url in sorted(urls):
            source = self.documents_by_url[url]
            filename = url_to_filename[url]
            target_urls = sorted(targets_by_source.get(url, set()))
            references = "\n".join(f"- [[{url_to_filename[target]}]]" for target in target_urls)
            reference_section = (
                f"## Evaluation graph references\n\n{references}\n\n" if references else ""
            )
            payload = (
                f"# {str(source.get('title', '')).strip()}\n\n"
                f"Source URL: {url}\n\n"
                f"{reference_section}"
                f"## Snapshot content\n\n{str(source.get('content', '')).strip()}\n"
            ).encode("utf-8")
            try:
                (root / filename).write_bytes(payload)
            except OSError as exc:
                raise EvaluationError("Cannot write an evaluation document") from exc
            manifest_documents.append(
                {
                    "url": url,
                    "title": str(source.get("title", "")),
                    "filename": filename,
                    "sha256": _sha256(payload),
                    "graph_targets": [url_to_filename[target] for target in target_urls],
                }
            )

        materialization_hash = _sha256(
            json.dumps(manifest_documents, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        manifest = {
            "schema_version": 1,
            "dataset_id": self.dataset_id,
            "source_sha256": self.fixture["source"]["sha256"],
            "materialization_sha256": materialization_hash,
            "document_count": len(manifest_documents),
            "documents": manifest_documents,
        }
        manifest_path = root / "manifest.json"
        try:
            manifest_path.write_bytes(
                (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            )
        except OSError as exc:
            raise EvaluationError("Cannot write the evaluation manifest") from exc
        return MaterializedEvaluation(
            root=root,
            manifest_path=manifest_path,
            dataset_id=self.dataset_id,
            materialization_sha256=materialization_hash,
            url_to_filename=url_to_filename,
        )


class EvaluationRunner:
    THRESHOLDS = {"direct": 27, "graph": 8, "multi_document": 4, "abstain": 5}

    def __init__(
        self,
        dataset: FrozenEvaluationDataset,
        materialized: MaterializedEvaluation,
        answers: AnswerService,
        *,
        embedding_identity: dict[str, object],
        provider_identity: dict[str, object],
        retrieval_configuration: dict[str, object] | None = None,
        label: str,
    ) -> None:
        self.dataset = dataset
        self.materialized = materialized
        self.answers = answers
        self.embedding_identity = embedding_identity
        self.provider_identity = provider_identity
        self.retrieval_configuration = retrieval_configuration or {}
        self.label = label

    def _url(self, evidence: Evidence) -> str | None:
        return self.materialized.filename_to_url.get(evidence.display_name)

    def evaluate_case(self, case: dict[str, Any]) -> dict[str, Any]:
        result, retrieved = self.answers.query_with_evidence(str(case["question"]))
        expected_urls = {str(item["url"]) for item in case.get("evidence", [])}
        retrieved_urls = {url for item in retrieved if (url := self._url(item)) is not None}
        graph_urls = {
            url
            for item in retrieved
            if item.via == "graph" and (url := self._url(item)) is not None
        }
        target_urls = {str(edge["to_url"]) for edge in case.get("graph_edges", [])}
        cited_urls = {
            url for item in result.citations if (url := self._url(item)) is not None
        }
        expected_outcome = str(case["expected_outcome"])
        required_terms = [str(term) for term in case.get("required_answer_terms", [])]
        terms_present = all(term.casefold() in result.answer.casefold() for term in required_terms)
        if expected_outcome == "abstain":
            retrieval_pass = None
            citation_pass = not result.citations
            answer_pass = (
                result.outcome is Outcome.INSUFFICIENT_EVIDENCE and citation_pass
            )
        else:
            retrieval_pass = expected_urls <= retrieved_urls and target_urls <= graph_urls
            citation_pass = expected_urls <= cited_urls
            answer_pass = (
                result.outcome is Outcome.ANSWER and terms_present and citation_pass
            )
        source_recall = (
            len(expected_urls & retrieved_urls) / len(expected_urls) if expected_urls else 1.0
        )
        return {
            "id": str(case["id"]),
            "category": str(case["category"]),
            "question": str(case["question"]),
            "expected_outcome": expected_outcome,
            "actual_outcome": result.outcome.value,
            "reference_answer": str(case["reference_answer"]),
            "answer": result.answer,
            "required_terms": required_terms,
            "required_terms_present": terms_present,
            "expected_urls": sorted(expected_urls),
            "retrieved_urls": sorted(retrieved_urls),
            "graph_retrieved_urls": sorted(graph_urls),
            "cited_urls": sorted(cited_urls),
            "source_recall": source_recall,
            "graph_target_recalled": target_urls <= graph_urls if target_urls else None,
            "retrieval_pass": retrieval_pass,
            "citation_pass": citation_pass,
            "answer_pass": answer_pass,
            "verification": dict(result.verification),
            "evidence": [
                {
                    "display_name": item.display_name,
                    "url": self._url(item),
                    "locator": item.locator,
                    "via": item.via,
                    "score": item.score,
                    "citation_id": item.citation_id,
                }
                for item in retrieved
            ],
        }

    def run(self, report_path: Path) -> dict[str, Any]:
        details = [self.evaluate_case(case) for case in self.dataset.cases]
        categories = Counter(item["category"] for item in details)
        category_retrieval_passes = Counter(
            item["category"] for item in details if bool(item["retrieval_pass"])
        )
        category_passes = Counter(
            item["category"] for item in details if bool(item["answer_pass"])
        )
        answerable = [item for item in details if item["expected_outcome"] == "answer"]
        graph_cases = [item for item in details if item["category"] == "graph"]
        durations = sorted(
            float(item["verification"].get("duration_ms", 0.0)) for item in details
        )
        p95_index = max(0, min(len(durations) - 1, int(len(durations) * 0.95) - 1))
        summary = {
            "total_cases": len(details),
            "retrieval_case_count": sum(
                item["retrieval_pass"] is not None for item in details
            ),
            "retrieval_passes": sum(item["retrieval_pass"] is True for item in details),
            "answer_passes": sum(bool(item["answer_pass"]) for item in details),
            "error_cases": sum(item["actual_outcome"] == "error" for item in details),
            "provider_calls": sum(
                int(item["verification"].get("provider_calls", 0)) for item in details
            ),
            "average_duration_ms": sum(durations) / len(durations) if durations else 0.0,
            "p95_duration_ms": durations[p95_index] if durations else 0.0,
            "source_recall": (
                sum(float(item["source_recall"]) for item in answerable) / len(answerable)
                if answerable
                else 0.0
            ),
            "graph_target_recall": (
                sum(bool(item["graph_target_recalled"]) for item in graph_cases)
                / len(graph_cases)
                if graph_cases
                else 0.0
            ),
            "citation_support_rate": (
                sum(bool(item["citation_pass"]) for item in answerable) / len(answerable)
                if answerable
                else 0.0
            ),
            "category_totals": dict(sorted(categories.items())),
            "category_retrieval_passes": dict(sorted(category_retrieval_passes.items())),
            "category_answer_passes": dict(sorted(category_passes.items())),
            "thresholds": dict(self.THRESHOLDS),
        }
        summary["gate_passed"] = all(
            category_passes.get(category, 0) >= minimum
            for category, minimum in self.THRESHOLDS.items()
        )
        report = {
            "schema_version": 1,
            "dataset_id": self.dataset.dataset_id,
            "materialization_sha256": self.materialized.materialization_sha256,
            "label": self.label,
            "generated_at": datetime.now(UTC).isoformat(),
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "cpu_count": os.cpu_count(),
            },
            "embedding": self.embedding_identity,
            "answer_provider": self.provider_identity,
            "retrieval": self.retrieval_configuration,
            "summary": summary,
            "details": details,
            "manual_review_required": (
                "Automatic scoring verifies required terms and expected citation coverage; "
                "review returned answers for unsupported additional claims before accepting P4."
            ),
        }
        path = report_path.resolve()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(
                (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            )
            os.replace(temporary, path)
        except OSError as exc:
            raise EvaluationError("Cannot write the evaluation report") from exc
        return report

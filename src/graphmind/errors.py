"""Typed errors used across CLI, services, and future HTTP/MCP adapters."""


class GraphMindError(Exception):
    code = "graphmind_error"


class ConfigurationError(GraphMindError):
    code = "configuration_error"


class PathValidationError(GraphMindError):
    code = "invalid_path"


class UnsupportedFileError(GraphMindError):
    code = "unsupported_file"


class EmptyDocumentError(GraphMindError):
    code = "empty_document"


class DocumentTooLargeError(GraphMindError):
    code = "document_too_large"


class DocumentEncodingError(GraphMindError):
    code = "document_encoding_error"


class DocumentParseError(GraphMindError):
    code = "document_parse_error"


class EncryptedDocumentError(DocumentParseError):
    code = "encrypted_document"


class OcrRequiredError(DocumentParseError):
    code = "ocr_required"


class ParserLimitError(DocumentParseError):
    code = "parser_limit"


class ParserTimeoutError(DocumentParseError):
    code = "parser_timeout"


class DocumentNotFoundError(GraphMindError):
    code = "document_not_found"


class DocumentBusyError(GraphMindError):
    code = "document_busy"


class MigrationError(GraphMindError):
    code = "migration_error"


class MigrationBusyError(MigrationError):
    code = "migration_busy"


class UnsupportedSchemaError(MigrationError):
    code = "unsupported_schema"


class JobLeaseError(GraphMindError):
    code = "job_lease_error"


class JobQueueFullError(GraphMindError):
    code = "job_queue_full"


class DependencyUnavailableError(GraphMindError):
    code = "dependency_unavailable"


class PublicationError(GraphMindError):
    code = "publication_error"


class ManifestUnavailableError(GraphMindError):
    code = "manifest_unavailable"


class RetryableQueryError(GraphMindError):
    code = "retryable_query_error"


class ProviderError(GraphMindError):
    code = "provider_error"


class ProviderResponseError(ProviderError):
    code = "invalid_provider_response"


class ProviderAuthenticationError(ProviderError):
    code = "provider_authentication_error"


class ProviderRateLimitError(ProviderError):
    code = "provider_rate_limited"


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"


class ProviderUnavailableError(ProviderError):
    code = "provider_unavailable"


class ModelNotFoundError(ProviderError):
    code = "model_not_found"


class EmbeddingError(GraphMindError):
    code = "embedding_error"


class EmbeddingUnavailableError(EmbeddingError):
    code = "embedding_unavailable"


class EmbeddingModelNotFoundError(EmbeddingError):
    code = "embedding_model_not_found"


class EmbeddingResponseError(EmbeddingError):
    code = "invalid_embedding_response"


class EmbeddingDimensionError(EmbeddingError):
    code = "embedding_dimension_mismatch"


class EmbeddingMismatchError(EmbeddingError):
    code = "embedding_fingerprint_mismatch"


class QueryBudgetError(GraphMindError):
    code = "query_budget_exceeded"


class QueryCapacityError(GraphMindError):
    code = "query_capacity_exceeded"


class QueryCancelledError(GraphMindError):
    code = "query_cancelled"


class EvaluationError(GraphMindError):
    code = "evaluation_error"

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

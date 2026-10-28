class PipelineError(Exception):
    """Base class for errors the graph converts into a failed GenerationResult."""


class EmbeddingNotAllowed(PipelineError):
    """complete_json must not be used for embeddings. The backend owns those calls."""


class DeadlineExceeded(PipelineError):
    """The internal 270s deadline is too close to start or finish a model call."""


class ModelCallError(PipelineError):
    """Provider error, invalid JSON, or HTTP failure after the allowed retry."""

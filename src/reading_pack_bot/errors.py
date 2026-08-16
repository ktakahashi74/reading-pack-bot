"""Public error hierarchy."""


class ReadingPackBotError(Exception):
    """Base class for expected application failures."""


class ConfigurationError(ReadingPackBotError):
    """Configuration is missing, unsafe, or inconsistent."""


class PackValidationError(ReadingPackBotError):
    """A Reading Pack artifact failed the consumer contract."""


class ProviderError(ReadingPackBotError):
    """A model provider failed without exposing request contents."""


class StoreError(ReadingPackBotError):
    """Conversation or idempotency state could not be persisted."""

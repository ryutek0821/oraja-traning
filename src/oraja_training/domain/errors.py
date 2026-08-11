"""Exceptions shared by database-independent core code and adapters.

The core deliberately exposes a small exception hierarchy.  Adapters may
translate their native errors into ``RepositoryError`` while callers can
handle all domain failures through ``DomainError``.
"""


class DomainError(RuntimeError):
    """Base class for an invalid or unusable domain operation."""


class CoreContractError(DomainError):
    """Raised when an adapter cannot satisfy a core contract."""


class DomainValidationError(CoreContractError):
    """Raised when a value crossing the core boundary is malformed."""


class RepositoryError(DomainError):
    """Raised when a persistence adapter cannot complete an operation."""

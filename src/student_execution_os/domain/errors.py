class DomainError(Exception):
    """Base class for domain/persistence contract failures."""


class ValidationError(DomainError):
    """Input violates a canonical domain invariant."""


class EntityNotFound(DomainError):
    """Entity is absent in the caller's account scope."""


class VersionConflict(DomainError):
    """Optimistic-concurrency precondition does not match current version."""


class UnsupportedCapability(DomainError):
    """Input is valid in the full specification but unsupported by this release."""


class DependencyCycleError(ValidationError):
    """A hard task dependency would make the dependency graph cyclic."""


class DuplicateHardCutoffOwner(ValidationError):
    """A final hard cutoff would gain two independently mutable owners."""

class AuthorizationDenied(DomainError):
    """Authenticated principal lacks a server-bound authorization for the action."""


class IdempotencyConflict(DomainError):
    """An idempotency key was reused for a different semantic action request."""

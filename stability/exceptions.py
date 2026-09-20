"""Domain-level errors raised by the service layer."""


class StabilityError(Exception):
    """Base class for business-rule violations (HTTP 400/409 at the view)."""


class PermissionDeniedError(StabilityError):
    """The authenticated user may not perform this action."""


class ValidationError(StabilityError):
    """Invalid input or state transition."""


class SampleUnavailableError(ValidationError):
    """Sample is consumed / terminal / already otherwise assigned."""


class FrozenError(ValidationError):
    """Result is frozen pending an impact assessment and cannot be decided."""

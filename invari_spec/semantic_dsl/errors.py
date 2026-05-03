from __future__ import annotations


class DslError(Exception):
    """Base class for semantic DSL errors."""


class DslParseError(DslError):
    """Raised when DSL source uses unsupported Python syntax or malformed calls."""


class DslTypeError(DslError):
    """Raised when DSL references or declarations fail semantic validation."""


class DslLoweringError(DslError):
    """Raised when a parsed DSL model cannot be lowered to TLA+."""

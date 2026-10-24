"""Failures phrased for the caller rather than as tracebacks."""

from __future__ import annotations


class ToolFailure(Exception):
    """A message meant for Claude, not a stack trace."""


class BackendError(RuntimeError):
    """A backend-side failure phrased for the caller."""

"""Shared exception for the controller and sender pipeline modules.

Defined in its own module so both pipeline implementations can raise
the same class and ``*_main`` adapters can catch a single type.
"""

from __future__ import annotations


class PipelineError(RuntimeError):
    """Raised when a GStreamer pipeline cannot be configured or run."""

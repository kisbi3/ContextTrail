"""Read-only transcript adapters. This module never runs recorded commands."""
from .local import collect_logs

__all__ = ["collect_logs"]

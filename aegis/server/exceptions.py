"""Aegis-specific exceptions."""
from __future__ import annotations


class AegisError(Exception):
    """Base class for Aegis server errors."""


class ConfigError(AegisError):
    """Configuration loading / validation failed."""


class QuotaExceededError(AegisError):
    """Org / project quota exceeded."""


class BrainPipelineError(AegisError):
    """Brain pipeline encountered an unrecoverable error."""


class PluginLoadError(AegisError):
    """AutoHeal plugin failed to load or validate."""


class EngineExecutionError(AegisError):
    """Engine failed to execute a plugin action."""

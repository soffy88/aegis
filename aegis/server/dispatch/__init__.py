"""Aegis service-layer dispatcher for omodul invocations."""

from aegis.server.dispatch.omodul_dispatcher import BudgetExceededError, OmodulDispatcher

__all__ = ["BudgetExceededError", "OmodulDispatcher"]

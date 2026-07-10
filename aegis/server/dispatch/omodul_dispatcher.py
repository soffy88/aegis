"""Service-layer dispatcher: orchestrates omodul invocations per Step 15 §2.5.

Responsibilities (additive to omodul, never replacing):
- Compute fingerprint via omodul.compute_fingerprint_for (not self-computed)
- Dedup check (short TTL cache)
- Budget check (per-user monthly)
- Build output_dir containing user_id (omodul doesn't know user_id)
- Invoke omodul function
- Persist decision_trail to Postgres (additive, omodul still writes its own JSON)
- Cache completed results for dedup
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import omodul

from aegis.server.dispatch.budget_tracker import BudgetTracker
from aegis.server.dispatch.dedup_cache import DedupCache

log = logging.getLogger(__name__)


class BudgetExceededError(Exception):
    """User budget for omodul calls exceeded."""


class OmodulDispatcher:
    """Dispatcher pattern per Step 15 §2.5."""

    def __init__(
        self,
        dedup_cache: DedupCache,
        budget_tracker: BudgetTracker,
        data_dir: str = "/var/lib/aegis",
    ) -> None:
        self.dedup_cache = dedup_cache
        self.budget_tracker = budget_tracker
        self.data_dir = data_dir
        # fingerprint -> in-flight execution task, so concurrent invoke() calls
        # with the same fingerprint (e.g. a retried alert) join the first
        # call's result instead of starting a duplicate (costly) execution.
        self._inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}

    async def invoke(
        self,
        omodul_name: str,
        config: dict[str, Any],
        input_data: dict[str, Any],
        user_id: str,
        on_step: Callable[[dict[str, Any]], None] | None = None,
        project_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Invoke an omodul with dispatcher orchestration."""
        # 1. Resolve function + Config/Input classes
        omodul_fn = getattr(omodul, omodul_name, None)
        if omodul_fn is None:
            raise ValueError(f"omodul '{omodul_name}' not found in main lib")

        config_cls = self._resolve_class(omodul_name, "Config")
        input_cls = self._resolve_class(omodul_name, "Input")

        config_obj = config_cls(**config)
        input_obj = input_cls(**input_data)

        # 2. Fingerprint (call omodul's own function, never self-compute)
        fp = omodul.compute_fingerprint_for(omodul_name, config_obj, input_obj)

        # 3. Dedup check
        cached = await self.dedup_cache.get(fp)
        if cached is not None:
            log.info("omodul_dedup_hit omodul=%s fp=%s", omodul_name, fp[:12])
            return cached

        # 3b. In-flight join: a concurrent call with the same fingerprint is
        # already executing — await its result instead of duplicating it.
        inflight = self._inflight.get(fp)
        if inflight is not None:
            log.info("omodul_inflight_join omodul=%s fp=%s", omodul_name, fp[:12])
            return await inflight

        task = asyncio.ensure_future(
            self._execute(
                omodul_name=omodul_name,
                omodul_fn=omodul_fn,
                config_obj=config_obj,
                input_obj=input_obj,
                user_id=user_id,
                on_step=on_step,
                project_id=project_id,
                fp=fp,
            )
        )
        self._inflight[fp] = task
        try:
            return await task
        finally:
            self._inflight.pop(fp, None)

    async def _execute(
        self,
        *,
        omodul_name: str,
        omodul_fn: Callable[..., dict[str, Any]],
        config_obj: Any,
        input_obj: Any,
        user_id: str,
        on_step: Callable[[dict[str, Any]], None] | None,
        project_id: uuid.UUID | None,
        fp: str,
    ) -> dict[str, Any]:
        """Budget-gate, invoke, and persist a single omodul execution."""
        # 4. Budget gate — atomically RESERVE this call's max budget BEFORE running.
        #    deduct() is the enforcement point: it check-and-increments in one
        #    WATCH/MULTI/EXEC transaction, so N concurrent invokes for a user at the
        #    limit can't all slip through (the old has_budget() read-then-act pre-check
        #    was racy and its result was the only gate). We reserve budget_usd (the
        #    call's cap) up front and reconcile to the real cost in the finally block.
        budget_usd = getattr(config_obj, "budget_usd", 5.0)
        if not await self.budget_tracker.deduct(user_id, budget_usd):
            raise BudgetExceededError(f"user {user_id} budget exceeded")

        result: dict[str, Any] | None = None
        try:
            # 5. Build output_dir with user_id
            output_dir = Path(self.data_dir) / "omodul_output" / user_id / omodul_name / fp
            output_dir.mkdir(parents=True, exist_ok=True)

            # 6. Invoke omodul (sync, does LLM HTTP calls — offload to a thread so
            # it doesn't block the event loop).
            log.info("omodul_invoke omodul=%s fp=%s user=%s", omodul_name, fp[:12], user_id)
            result = await asyncio.to_thread(
                omodul_fn, config_obj, input_obj, output_dir, on_step=on_step
            )

            # 7. Persist decision_trail (additive)
            from aegis.server.persistence.event_trail import save_decision_trail

            await save_decision_trail(
                omodul_name=omodul_name,
                fingerprint=fp,
                decision_trail=result.get("decision_trail", {}),
                user_id=user_id,
                status=result.get("status", "unknown"),
                error=result.get("error"),
                project_id=project_id,
            )

            # 8. Dedup cache (only on success)
            if result.get("status") == "completed":
                await self.dedup_cache.set(fp, result)

            # 8b. Persist the charge to the per-org cost ledger (best-effort).
            from aegis.server.services.llm_cost import record_cost  # noqa: PLC0415

            await record_cost(
                principal=user_id,
                omodul_name=omodul_name,
                model=result.get("model", ""),
                cost_usd=result.get("cost_usd", 0.0),
            )

            return result
        finally:
            # 9. Reconcile the reservation to actual spend: refund the unused
            #    portion (or charge overage). On failure result is None → actual 0,
            #    fully refunding the reservation so a crashed run costs nothing.
            actual_cost = float(result.get("cost_usd", 0.0)) if result else 0.0
            await self.budget_tracker.settle(
                user_id, reserved_usd=budget_usd, actual_usd=actual_cost
            )

    def _resolve_class(self, omodul_name: str, suffix: str) -> type:
        """Resolve Config/Input class from omodul submodule."""
        pascal = "".join(p.capitalize() for p in omodul_name.split("_"))
        class_name = f"{pascal}{suffix}"

        # Try omodul top-level first
        cls = getattr(omodul, class_name, None)
        if cls is not None:
            return cls

        # Fall back to submodule import
        mod = importlib.import_module(f"omodul.{omodul_name}")
        cls = getattr(mod, class_name, None)
        if cls is None:
            raise ValueError(f"{class_name} not found in omodul.{omodul_name}")
        return cls

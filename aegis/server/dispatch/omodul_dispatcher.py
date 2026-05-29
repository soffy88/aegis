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

        # 4. Budget check
        budget_usd = getattr(config_obj, "budget_usd", 5.0)
        if not await self.budget_tracker.has_budget(user_id, budget_usd):
            raise BudgetExceededError(f"user {user_id} budget exceeded")

        # 5. Build output_dir with user_id
        output_dir = Path(self.data_dir) / "omodul_output" / user_id / omodul_name / fp
        output_dir.mkdir(parents=True, exist_ok=True)

        # 6. Invoke omodul
        log.info("omodul_invoke omodul=%s fp=%s user=%s", omodul_name, fp[:12], user_id)
        result: dict[str, Any] = omodul_fn(config_obj, input_obj, output_dir, on_step=on_step)

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

        # 9. Budget deduct
        await self.budget_tracker.deduct(user_id, result.get("cost_usd", 0.0))

        return result

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

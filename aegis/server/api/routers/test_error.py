"""Test error endpoint — C3-7 e2e verification (dev/test only).

Registered by app.py only when ENV != 'prod'.
Triggers a Python exception that sentry-python captures and sends to the
Aegis envelope endpoint, verifying the full C3 pipeline.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/test", tags=["test"])

_VALID_TYPES = {"ValueError", "TypeError", "RuntimeError"}


@router.post("/error/")
async def trigger_test_error(error_type: str = "ValueError") -> dict:
    """Raise a test exception (captured by sentry-sdk if init'd).

    Query param error_type: ValueError (default) / TypeError / RuntimeError.
    Never registered in prod (ENV=prod).
    """
    if error_type == "ValueError":
        raise ValueError("test error from Aegis self-monitoring (C3-7 e2e)")
    if error_type == "TypeError":
        raise TypeError("test type error from Aegis self-monitoring")
    if error_type == "RuntimeError":
        raise RuntimeError("test runtime error from Aegis self-monitoring")
    raise ValueError(f"test error [{error_type}] from Aegis self-monitoring")

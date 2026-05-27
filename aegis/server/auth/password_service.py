"""argon2id password hash + verify.

M1 uses passlib defaults: memory_cost=65536 KB, time_cost=3, parallelism=4.
OWASP-recommended. No memory pressure on 32 GB Wiki WSL2.
"""

from __future__ import annotations

from passlib.context import CryptContext

_ctx = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _ctx.verify(plain, hashed)
    except Exception:
        return False

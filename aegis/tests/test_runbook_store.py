"""Tests for the Postgres-backed runbook RAG store (pgvector + pg_trgm, no LanceDB)."""

from __future__ import annotations

from unittest import mock

import pytest

from aegis.server.services import runbook_store as rs
from aegis.server.services.runbook import Runbook, RunbookStep, StepType


def _rb(name: str, desc: str) -> Runbook:
    return Runbook(
        name=name,
        description=desc,
        trigger="manual",
        steps=[RunbookStep(name="restart", type=StepType.docker, command="restart x")],
    )


def test_vec_literal_roundtrip() -> None:
    v = [0.1, -0.2, 0.3]
    assert rs._parse_vec(rs._vec_literal(v)) == pytest.approx(v)
    assert rs._parse_vec(None) is None


def test_cosine() -> None:
    assert rs._cosine([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)
    assert rs._cosine([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)
    assert rs._cosine([1, 0], [1, 2, 3]) == 0.0  # dim mismatch → 0


def test_retrieve_semantic_ranks_by_cosine() -> None:
    rs._INDEX = [
        {
            "name": "a",
            "title": "A",
            "content": "restart nginx",
            "tags": [],
            "embedding": [1.0, 0.0],
        },
        {"name": "b", "title": "B", "content": "clear cache", "tags": [], "embedding": [0.0, 1.0]},
    ]
    embedder = mock.Mock(return_value=[[0.9, 0.1]])  # closest to 'a'
    out = rs.retrieve("nginx down", top_k=2, min_score=0.0, embedder=embedder)
    assert [r["runbook_id"] for r in out] == ["a", "b"]
    assert out[0]["score"] > out[1]["score"]


def test_retrieve_lexical_floor_when_no_embedder() -> None:
    """No embedder → token-overlap lexical ranking still returns the relevant runbook."""
    rs._INDEX = [
        {
            "name": "a",
            "title": "A",
            "content": "restart nginx container",
            "tags": [],
            "embedding": None,
        },
        {
            "name": "b",
            "title": "B",
            "content": "rotate database credentials",
            "tags": [],
            "embedding": None,
        },
    ]
    out = rs.retrieve("nginx restart needed", top_k=1, min_score=0.5, embedder=None)
    assert out and out[0]["runbook_id"] == "a"


def test_retrieve_empty_index() -> None:
    rs._INDEX = []
    assert rs.retrieve("anything", top_k=5, min_score=0.0, embedder=None) == []


def test_retrieve_embed_failure_degrades_to_lexical() -> None:
    rs._INDEX = [
        {
            "name": "a",
            "title": "A",
            "content": "restart nginx",
            "tags": [],
            "embedding": [1.0, 0.0],
        },
    ]
    boom = mock.Mock(side_effect=RuntimeError("model load failed"))
    out = rs.retrieve("restart nginx", top_k=1, min_score=0.0, embedder=boom)
    assert out and out[0]["runbook_id"] == "a"  # fell back to lexical, still found it


@pytest.mark.asyncio
async def test_sync_index_embeds_only_changed_and_prunes() -> None:
    conn = mock.AsyncMock()
    # existing-hashes query → 'a' present with matching hash+emb; reload query → rows
    rb_a, rb_b = _rb("a", "alpha"), _rb("b", "beta")
    _t, content_a = rs._content_for(rb_a)
    conn.fetch.side_effect = [
        [{"runbook_name": "a", "content_hash": rs._hash(content_a), "has_emb": True}],  # existing
        [  # reload_cache
            {
                "runbook_name": "a",
                "title": "a",
                "content": content_a,
                "tags": [],
                "requires_approval": True,
                "emb": None,
            },
            {
                "runbook_name": "b",
                "title": "b",
                "content": "b",
                "tags": [],
                "requires_approval": True,
                "emb": None,
            },
        ],
    ]
    embedder = mock.Mock(return_value=[[0.1, 0.2]])  # only 'b' is new → 1 vector

    n = await rs.sync_index(conn, [rb_a, rb_b], embedder)

    assert n == 2
    embedder.assert_called_once()  # only the changed runbook ('b') embedded
    assert embedder.call_args.args[0] == [rs._content_for(rb_b)[1]]
    # a DELETE prune ran with the desired names
    prune = [c for c in conn.execute.call_args_list if "DELETE" in c.args[0]]
    assert prune and set(prune[0].args[1]) == {"a", "b"}

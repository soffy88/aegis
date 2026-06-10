"""Tests for aegis.server.services.vector_store."""

from unittest.mock import MagicMock, patch

import numpy as np

from aegis.server.services.vector_store import (
    make_vector_encode_fn,
    make_vector_search_fn,
)


def test_make_vector_encode_fn_returns_list() -> None:
    """encode fn 将 query str 转为 list[float]."""
    with patch(
        "aegis.server.services.vector_store.vector_encode",
        return_value=np.array([[0.1, 0.2, 0.3]], dtype="float32"),
    ):
        fn = make_vector_encode_fn(provider="default")
        result = fn("nginx OOM")
    assert isinstance(result, list)
    assert len(result) == 3
    assert all(isinstance(x, float) for x in result)


def test_make_vector_search_fn_maps_records_to_dicts() -> None:
    """search fn 把 VectorRecord 映射成 retrieve_runbook 期望的 dict 格式."""
    from oprim.vector_db.lancedb import VectorRecord

    mock_db = MagicMock()
    mock_db.search.return_value = [
        VectorRecord(
            id="rb-001",
            embedding=[0.1, 0.2],
            metadata={
                "title": "Restart Service",
                "content": "Step 1: restart",
                "tags": ["restart"],
                "score": 0.92,
            },
        )
    ]
    fn = make_vector_search_fn(db=mock_db, collection="runbooks")
    results = fn(vector=[0.1, 0.2], collection="runbooks", top_k=5)

    assert len(results) == 1
    assert results[0]["id"] == "rb-001"
    assert results[0]["title"] == "Restart Service"
    assert results[0]["score"] == 0.92
    assert results[0]["tags"] == ["restart"]


def test_make_vector_search_fn_handles_empty_results() -> None:
    mock_db = MagicMock()
    mock_db.search.return_value = []
    fn = make_vector_search_fn(db=mock_db, collection="runbooks")
    results = fn(vector=[0.1, 0.2], collection="runbooks", top_k=5)
    assert results == []

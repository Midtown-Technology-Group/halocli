from __future__ import annotations

import pytest

from halocli.errors import HaloCLIError, classify_error, diagnose_permission_failure
from halocli.utils import coerce_batch_response, list_all, normalize_halo_result


class DotDictLike(dict):
    pass


class ObjectPage:
    def __init__(self) -> None:
        self.clients = [{"id": 1}, {"id": 2}]
        self.record_count = 3


class StatusError(Exception):
    def __init__(self, status_code: int, text: str = "", retry_after: str | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.retry_after = retry_after
        super().__init__(f"HTTP {status_code}: {text}")


def test_normalize_halo_result_recurses_dicts_lists_and_objects() -> None:
    payload = DotDictLike({"ticket": DotDictLike({"id": 7}), "items": [DotDictLike({"id": 8})]})

    assert normalize_halo_result(payload) == {"ticket": {"id": 7}, "items": [{"id": 8}]}


@pytest.mark.asyncio
async def test_list_all_clamps_page_size_and_respects_max_records() -> None:
    calls: list[dict] = []

    async def fetch(**kwargs):
        calls.append(kwargs)
        if kwargs["page_no"] == 1:
            return ObjectPage()
        return {"clients": [{"id": 3}], "record_count": 3}

    rows = await list_all(fetch, page_size=500, max_records=2)

    assert rows == [{"id": 1}, {"id": 2}]
    assert calls[0]["page_size"] == 100


def test_coerce_batch_response_extracts_named_list() -> None:
    assert coerce_batch_response({"tickets": [{"id": 10}], "record_count": 1}) == [{"id": 10}]


@pytest.mark.parametrize(
    ("status_code", "category"),
    [(400, "validation"), (401, "auth"), (403, "permission"), (404, "not_found"), (429, "rate_limit"), (500, "server")],
)
def test_classify_error_maps_status_codes(status_code: int, category: str) -> None:
    err = classify_error(StatusError(status_code, "body", retry_after="9"), endpoint="/api/Client")

    assert isinstance(err, HaloCLIError)
    assert err.category == category
    if status_code == 429:
        assert err.retry_after == 9


def test_permission_diagnostic_mentions_feature_access_for_client_403() -> None:
    err = classify_error(StatusError(403, "Forbidden"), endpoint="/api/Client")

    assert "feature-access" in diagnose_permission_failure(err)

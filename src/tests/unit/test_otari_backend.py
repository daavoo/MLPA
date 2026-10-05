"""GATEWAY_BACKEND=otari: Otari's error codes, and users, budgets and limits through its API."""

import json

import httpx
import pytest

from mlpa.core.config import (
    ERROR_CODE_BUDGET_LIMIT_EXCEEDED,
    ERROR_CODE_GLOBAL_BUDGET_LIMIT_EXCEEDED,
    ERROR_CODE_INVALID_MODEL_NAME,
    ERROR_CODE_RATE_LIMIT_EXCEEDED,
    ERROR_CODE_REQUEST_TOO_LARGE,
    ERROR_CODE_UPSTREAM_RATE_LIMIT_EXCEEDED,
    OTARI_API_ROOT,
    env,
)
from mlpa.core.errors import (
    classify_otari_stream_error,
    classify_upstream_error,
    is_otari_user_blocked,
)
from mlpa.core.services.otari_service import OtariService, duration_seconds


def _classify(code: str, status: int = 429, **headers: str):
    return classify_upstream_error(
        error_text='{"detail": "reworded at will"}',
        status_code=status,
        user="u:ai",
        headers=httpx.Headers({"Otari-Error-Code": code, **headers}),
    )


@pytest.mark.parametrize(
    ("code", "scope", "status", "expected", "http_status"),
    [
        ("budget_exceeded", "user", 403, ERROR_CODE_BUDGET_LIMIT_EXCEEDED, 429),
        (
            "budget_exceeded",
            "api_token",
            403,
            ERROR_CODE_GLOBAL_BUDGET_LIMIT_EXCEEDED,
            500,
        ),
        ("rate_limited", None, 429, ERROR_CODE_RATE_LIMIT_EXCEEDED, 429),
        (
            "upstream_rate_limited",
            None,
            429,
            ERROR_CODE_UPSTREAM_RATE_LIMIT_EXCEEDED,
            429,
        ),
        ("invalid_model", None, 400, ERROR_CODE_INVALID_MODEL_NAME, 400),
    ],
)
def test_otari_codes_map_without_reading_the_text(
    code, scope, status, expected, http_status
):
    headers = {"Otari-Budget-Scope": scope} if scope else {}
    match = _classify(code, status, **headers)

    assert match is not None
    assert match.error_code == expected
    assert match.http_status == http_status


def test_otari_retry_after_is_kept():
    match = _classify("rate_limited", **{"Retry-After": "17"})

    assert match is not None
    assert match.retry_after == "17"


def test_a_blocked_end_user_is_recognized():
    assert is_otari_user_blocked(httpx.Headers({"Otari-Error-Code": "user_blocked"}))
    assert not is_otari_user_blocked(httpx.Headers({}))


def test_litellm_durations_convert_to_seconds():
    assert duration_seconds("1d") == 86400
    assert duration_seconds("7d") == 604800
    assert duration_seconds("30m") == 1800


@pytest.fixture
async def otari():
    service = OtariService()
    await service.connect()
    yield service
    await service.disconnect()


async def test_an_end_user_is_found_by_owner_and_external_id(otari, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url=httpx.URL(
            f"{OTARI_API_ROOT}/users",
            params={"parent_user_id": "mlpa-ai", "external_id": "fxa1:ai", "limit": 1},
        ),
        json=[
            {
                "user_id": "eu_1",
                "external_id": "fxa1:ai",
                "blocked": True,
                "spend": 0.02,
                "budget_id": "b-ai",
            }
        ],
    )
    otari._budget_names["b-ai"] = "end-user-budget-ai"

    user = await otari.get_user("fxa1:ai")

    assert user is not None
    assert user["user_id"] == "fxa1:ai"
    assert user["otari_user_id"] == "eu_1"
    assert user["budget_id"] == "end-user-budget-ai"
    assert user["blocked"] is True
    assert otari.is_known("fxa1:ai")


async def test_blocking_patches_the_otari_user(otari, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url=httpx.URL(
            f"{OTARI_API_ROOT}/users",
            params={
                "parent_user_id": "mlpa-memories",
                "external_id": "fxa1:memories",
                "limit": 1,
            },
        ),
        json=[{"user_id": "eu_2", "external_id": "fxa1:memories"}],
    )
    httpx_mock.add_response(
        method="PATCH",
        url=f"{OTARI_API_ROOT}/users/eu_2",
        json={"user_id": "eu_2", "external_id": "fxa1:memories", "blocked": True},
    )

    user = await otari.block_user("fxa1:memories")

    assert user["blocked"] is True
    assert json.loads(httpx_mock.get_requests()[-1].content) == {"blocked": True}


_AI_CONFIG = {
    "ai": {
        "feature": "smart-window",
        "budget_id": "end-user-budget-ai",
        "budget_duration": "1d",
        "max_budget": 0.1,
        "rpm_limit": 40,
        "tpm_limit": 2000,
    }
}


@pytest.fixture
def ai_only(monkeypatch):
    # service_type_config is a cached_property, so the cached value is what to replace.
    monkeypatch.setitem(env.__dict__, "service_type_config", _AI_CONFIG)


async def test_provisioning_puts_the_per_user_limits_on_the_budget(
    otari, httpx_mock, ai_only
):
    httpx_mock.add_response(
        method="GET", url=f"{OTARI_API_ROOT}/budgets?limit=1000", json=[]
    )
    httpx_mock.add_response(
        method="POST", url=f"{OTARI_API_ROOT}/budgets", json={"budget_id": "b-ai"}
    )
    httpx_mock.add_response(
        method="GET", url=f"{OTARI_API_ROOT}/keys?limit=1000", json=[]
    )
    httpx_mock.add_response(
        method="POST", url=f"{OTARI_API_ROOT}/keys", json={"key": "sk-ai"}
    )

    secrets = await otari.provision()

    budget, key = (
        json.loads(r.content) for r in httpx_mock.get_requests() if r.method == "POST"
    )
    assert budget == {
        "name": "end-user-budget-ai",
        "max_budget": 0.1,
        "budget_duration_sec": 86400,
        "rpm_limit": 40,
        "tpm_limit": 2000,
    }
    assert key == {
        "key_name": "mlpa-ai",
        "user_id": "mlpa-ai",
        "is_service_key": True,
        "end_user_budget_id": "b-ai",
    }
    assert secrets == {"ai": "sk-ai"}


async def test_provisioning_refuses_duplicate_budgets(otari, httpx_mock, ai_only):
    duplicate = {"name": "end-user-budget-ai", "budget_id": "b"}
    httpx_mock.add_response(
        method="GET",
        url=f"{OTARI_API_ROOT}/budgets?limit=1000",
        json=[duplicate, duplicate],
    )

    with pytest.raises(RuntimeError, match="2 budgets named"):
        await otari.provision()


async def test_startup_only_reads(otari, httpx_mock, ai_only):
    httpx_mock.add_response(
        method="GET",
        url=f"{OTARI_API_ROOT}/budgets?limit=1000",
        json=[{"name": "end-user-budget-ai", "budget_id": "b-ai"}],
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{OTARI_API_ROOT}/keys?limit=1000",
        json=[{"key_name": "mlpa-ai", "id": "k"}],
    )

    await otari.create_budget()

    assert {r.method for r in httpx_mock.get_requests()} == {"GET"}
    assert otari._budget_ids == {"end-user-budget-ai": "b-ai"}


async def test_users_are_counted_from_the_total_header(
    otari, httpx_mock, ai_only, monkeypatch
):
    monkeypatch.setitem(env.__dict__, "valid_service_types", ["ai"])
    httpx_mock.add_response(
        method="GET",
        url=httpx.URL(
            f"{OTARI_API_ROOT}/users",
            params={"parent_user_id": "mlpa-ai", "limit": 1, "include_total": "true"},
        ),
        json=[{}],
        headers={"Otari-Total-Count": "42"},
    )

    assert await otari.count_users_by_service_type() == {
        "service_type_counts": {"ai": 42},
        "total_users": 42,
    }


def test_the_code_in_the_body_is_enough():
    match = classify_upstream_error(
        error_text=json.dumps(
            {"detail": "anything", "code": "context_length_exceeded"}
        ),
        status_code=400,
        user="u:ai",
    )

    assert match is not None
    assert match.error_code == ERROR_CODE_REQUEST_TOO_LARGE
    assert match.http_status == 413


def test_a_coded_stream_error_event_maps_to_mlpa_codes():
    event = {
        "error": {
            "message": "x",
            "type": "server_error",
            "code": "upstream_rate_limited",
        }
    }

    match = classify_otari_stream_error(event, "u:ai")

    assert match is not None
    assert match.error_code == ERROR_CODE_UPSTREAM_RATE_LIMIT_EXCEEDED
    assert classify_otari_stream_error({"choices": []}, "u:ai") is None

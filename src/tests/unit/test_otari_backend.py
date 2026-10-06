"""GATEWAY_BACKEND=otari: Otari's error codes, and users, budgets and limits through its API."""

import json

import httpx
import pytest

from mlpa.core import config as config_module
from mlpa.core.config import (
    ERROR_CODE_BUDGET_LIMIT_EXCEEDED,
    ERROR_CODE_GLOBAL_BUDGET_LIMIT_EXCEEDED,
    ERROR_CODE_INVALID_MODEL_NAME,
    ERROR_CODE_RATE_LIMIT_EXCEEDED,
    ERROR_CODE_REQUEST_TOO_LARGE,
    ERROR_CODE_UPSTREAM_RATE_LIMIT_EXCEEDED,
    OTARI_API_ROOT,
    env,
    resolve_litellm_virtual_auth_headers,
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


_KEY = {
    "id": "k1",
    "key_name": "mlpa",
    "is_active": True,
    "end_user_budget_ids": ["end-user-budget-ai"],
    "end_user_budget_id": "end-user-budget-ai",
}


async def test_an_end_user_is_read_by_the_id_mlpa_names_it_by(otari, httpx_mock):
    otari._key_id = "k1"
    httpx_mock.add_response(
        method="GET",
        url=f"{OTARI_API_ROOT}/keys/k1/end-users/fxa1%3Aai",
        json={
            "user_id": "eu_1",
            "external_id": "fxa1:ai",
            "blocked": True,
            "spend": 0.02,
            "budget_id": "end-user-budget-ai",
        },
    )

    user = await otari.get_user("fxa1:ai")

    assert user is not None
    assert user["user_id"] == "fxa1:ai"
    assert user["otari_user_id"] == "eu_1"
    assert user["budget_id"] == "end-user-budget-ai"
    assert user["blocked"] is True
    assert otari.is_known("fxa1:ai")


async def test_an_unknown_end_user_is_none(otari, httpx_mock):
    otari._key_id = "k1"
    httpx_mock.add_response(
        method="GET",
        url=f"{OTARI_API_ROOT}/keys/k1/end-users/new%3Aai",
        status_code=404,
        json={"detail": "End user 'new:ai' not found"},
    )

    assert await otari.get_user("new:ai") is None
    assert not otari.is_known("new:ai")


async def test_blocking_and_moving_patch_the_end_user(otari, httpx_mock):
    otari._key_id = "k1"
    url = f"{OTARI_API_ROOT}/keys/k1/end-users/fxa1%3Amemories"
    httpx_mock.add_response(
        method="PATCH",
        url=url,
        json={"user_id": "eu_2", "external_id": "fxa1:memories", "blocked": True},
    )
    httpx_mock.add_response(
        method="PATCH",
        url=url,
        json={"user_id": "eu_2", "external_id": "fxa1:memories", "budget_id": "b-dev"},
    )

    blocked = await otari.block_user("fxa1:memories")
    moved = await otari.update_user_budget("fxa1:memories", "b-dev")

    first, second = httpx_mock.get_requests()
    assert blocked["blocked"] is True
    assert json.loads(first.content) == {"blocked": True}
    assert moved["budget_id"] == "b-dev"
    assert json.loads(second.content) == {"budget_id": "b-dev"}


async def test_the_key_id_is_looked_up_once(otari, httpx_mock):
    httpx_mock.add_response(
        method="GET", url=f"{OTARI_API_ROOT}/keys?limit=1000", json=[_KEY]
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{OTARI_API_ROOT}/keys/k1/end-users/a%3Aai",
        json={"user_id": "eu_a", "external_id": "a:ai"},
        is_reusable=True,
    )

    await otari.get_user("a:ai")
    await otari.get_user("a:ai")

    assert [r.method for r in httpx_mock.get_requests()] == ["GET", "GET", "GET"]


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


async def test_provisioning_puts_budgets_by_id_and_one_key_listing_them(
    otari, httpx_mock, ai_only
):
    httpx_mock.add_response(
        method="PUT",
        url=f"{OTARI_API_ROOT}/budgets/end-user-budget-ai",
        json={"budget_id": "end-user-budget-ai"},
    )
    httpx_mock.add_response(
        method="GET", url=f"{OTARI_API_ROOT}/keys?limit=1000", json=[]
    )
    httpx_mock.add_response(
        method="POST", url=f"{OTARI_API_ROOT}/keys", json={"id": "k1", "key": "sk-1"}
    )

    secret = await otari.provision()

    put, _get, post = httpx_mock.get_requests()
    assert json.loads(put.content) == {
        "name": "end-user-budget-ai",
        "max_budget": 0.1,
        "budget_duration_sec": 86400,
        "rpm_limit": 40,
        "tpm_limit": 2000,
    }
    assert json.loads(post.content) == {
        "key_name": "mlpa",
        "user_id": "mlpa",
        "is_service_key": True,
        "end_user_budget_ids": ["end-user-budget-ai"],
        "end_user_budget_id": "end-user-budget-ai",
    }
    assert secret == "sk-1"
    assert otari._key_id == "k1"


async def test_provisioning_again_leaves_a_matching_key_alone(
    otari, httpx_mock, ai_only
):
    httpx_mock.add_response(
        method="PUT",
        url=f"{OTARI_API_ROOT}/budgets/end-user-budget-ai",
        json={"budget_id": "end-user-budget-ai"},
    )
    httpx_mock.add_response(
        method="GET", url=f"{OTARI_API_ROOT}/keys?limit=1000", json=[_KEY]
    )

    assert await otari.provision() is None
    assert {r.method for r in httpx_mock.get_requests()} == {"PUT", "GET"}


async def test_startup_only_reads(otari, httpx_mock, ai_only):
    httpx_mock.add_response(
        method="GET", url=f"{OTARI_API_ROOT}/keys?limit=1000", json=[_KEY]
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{OTARI_API_ROOT}/budgets?limit=1000",
        json=[{"budget_id": "end-user-budget-ai", "user_count": 0}],
    )

    await otari.create_budget()

    assert {r.method for r in httpx_mock.get_requests()} == {"GET"}
    assert otari._key_id == "k1"


async def test_users_are_counted_by_their_service_types_budget(
    otari, httpx_mock, ai_only
):
    httpx_mock.add_response(
        method="GET",
        url=f"{OTARI_API_ROOT}/budgets?limit=1000",
        json=[
            {"budget_id": "end-user-budget-ai", "user_count": 42},
            {"budget_id": "someone-elses", "user_count": 7},
        ],
    )

    assert await otari.count_users_by_service_type() == {
        "service_type_counts": {"ai": 42},
        "total_users": 42,
    }


async def test_users_are_listed_under_the_one_owner(otari, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url=httpx.URL(
            f"{OTARI_API_ROOT}/users",
            params={
                "parent_user_id": "mlpa",
                "skip": 10,
                "limit": 2,
                "include_total": "true",
            },
        ),
        json=[{"user_id": "eu_1", "external_id": "a:ai"}],
        headers={"Otari-Total-Count": "11"},
    )

    listed = await otari.list_users(limit=2, offset=10)

    assert listed["total"] == 11
    assert [user["user_id"] for user in listed["users"]] == ["a:ai"]


def test_requests_name_the_service_types_budget(monkeypatch, ai_only):
    monkeypatch.setattr(config_module, "USE_OTARI", True)
    monkeypatch.setattr(env, "OTARI_SERVICE_KEY", "sk-1")

    headers = resolve_litellm_virtual_auth_headers(service_type="ai")

    assert headers["Authorization"] == "Bearer sk-1"
    assert headers["otari-end-user-budget"] == "end-user-budget-ai"


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

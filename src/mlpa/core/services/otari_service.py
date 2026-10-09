"""MLPA's users, budgets and limits kept in Otari, through Otari's management API.

A drop-in for ``LiteLLMPGService`` when ``GATEWAY_BACKEND=otari``: the same
method names and return shapes, so the user router, the signup cap and startup
call it unchanged.

How MLPA's model maps onto Otari's:

- One Otari service key, owned by the Otari user ``OTARI_OWNER_USER``, serves
  every service type. A request's ``user`` ("<identity>:<service type>") names an
  end user of that owner, which Otari creates on its first request on the budget
  the ``Otari-End-User-Budget`` header names: the service type's.
- Each service type's budget is an Otari budget under MLPA's own budget id, which
  the key lists among the budgets it may assign.
- Each service type's per-user RPM and TPM are that budget's ``rpm_limit`` and
  ``tpm_limit``, so moving a user to another budget moves its limits too.
"""

import re
from collections import OrderedDict
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from mlpa.core.config import (
    LITELLM_MASTER_AUTH_HEADERS,
    LITELLM_READINESS_URL,
    OTARI_API_ROOT,
    env,
)
from mlpa.core.logger import logger

_DURATION = re.compile(r"^\s*(\d+)\s*(s|m|h|d|w|mo)\s*$")
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "mo": 2592000}
_PAGE = 1000


def duration_seconds(duration: str) -> int:
    """A LiteLLM budget duration ("1d", "7d", "30m") in seconds."""
    match = _DURATION.match(duration)
    if match is None:
        raise ValueError(f"Unrecognized budget duration {duration!r}")
    return int(match.group(1)) * _DURATION_SECONDS[match.group(2)]


# A whole number of days, weeks or months resets on that UTC calendar boundary, as
# MLPA's budgets are meant to; any other duration keeps a rolling period.
_CALENDAR_ALIGNMENT = {
    "1d": "calendar_day",
    "1w": "calendar_week",
    "7d": "calendar_week",
    "1mo": "calendar_month",
    "30d": "calendar_month",
}


def budget_period(duration: str) -> dict[str, str | int]:
    """The Otari budget fields for a LiteLLM budget duration: a UTC calendar reset where one matches."""
    alignment = _CALENDAR_ALIGNMENT.get(duration.replace(" ", ""))
    if alignment is not None:
        return {"reset_alignment": alignment}
    return {"budget_duration_sec": duration_seconds(duration)}


def budget_ids() -> list[str]:
    """MLPA's budget ids, one per service type, in service-type order and without repeats."""
    return list(dict.fromkeys(c["budget_id"] for c in env.service_type_config.values()))


class OtariService:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # The service key's id, which addresses its end users. Learned at startup.
        self._key_id: str | None = None
        self._known_users: OrderedDict[str, None] = OrderedDict()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("OtariService is not connected")
        return self._client

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=OTARI_API_ROOT,
            headers=LITELLM_MASTER_AUTH_HEADERS,
            timeout=httpx.Timeout(env.PG_ADMIN_READ_TIMEOUT_MS / 1000),
        )

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ping(self) -> bool:
        try:
            response = await self.client.get(
                LITELLM_READINESS_URL, timeout=env.READINESS_CHECK_TIMEOUT_S
            )
        except Exception:
            return False
        return response.status_code == 200

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    # Users

    def remember(self, user_id: str) -> None:
        """Note that ``user_id`` exists in Otari, so its next request skips the lookup."""
        self._known_users[user_id] = None
        self._known_users.move_to_end(user_id)
        while len(self._known_users) > env.OTARI_KNOWN_USERS_CACHE_SIZE:
            self._known_users.popitem(last=False)

    def is_known(self, user_id: str) -> bool:
        return user_id in self._known_users

    @staticmethod
    def _as_mlpa_user(user: dict[str, Any]) -> dict[str, Any]:
        """An Otari end user in the shape MLPA read from LiteLLM_EndUserTable."""
        return {
            "user_id": user.get("external_id"),
            "otari_user_id": user.get("user_id"),
            "budget_id": user.get("budget_id"),
            "blocked": bool(user.get("blocked")),
            "spend": user.get("spend", 0.0),
            "budget_started_at": user.get("budget_started_at"),
            "next_budget_reset_at": user.get("next_budget_reset_at"),
            "created_at": user.get("created_at"),
        }

    async def _key(self) -> str:
        if self._key_id is None:
            await self._learn_key()
        if self._key_id is None:
            raise RuntimeError(
                "Otari has no MLPA service key; run scripts/otari_provision.py"
            )
        return self._key_id

    async def _end_user(
        self, method: str, user_id: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        """One call on the end user MLPA names ``user_id``, or None when Otari has none."""
        path = f"/keys/{await self._key()}/end-users/{quote(user_id, safe='')}"
        response = await self.client.request(method, path, **kwargs)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        user = await self._end_user("GET", user_id)
        if user is None:
            return None
        self.remember(user_id)
        return self._as_mlpa_user(user)

    async def update_user_budget(self, user_id: str, budget_id: str) -> dict:
        updated = await self._end_user("PATCH", user_id, json={"budget_id": budget_id})
        if updated is None:
            raise HTTPException(status_code=404, detail="User not found.")
        logger.info(f"User {user_id} budget updated to {budget_id} successfully.")
        return self._as_mlpa_user(updated)

    async def block_user(self, user_id: str, blocked: bool = True) -> dict:
        updated = await self._end_user("PATCH", user_id, json={"blocked": blocked})
        if updated is None:
            raise HTTPException(status_code=404, detail="User not found.")
        logger.info(
            f"User {user_id} {'blocked' if blocked else 'unblocked'} successfully."
        )
        return self._as_mlpa_user(updated)

    async def list_users(self, limit: int = 50, offset: int = 0) -> dict:
        response = await self.client.get(
            "/users",
            params={
                "parent_user_id": env.OTARI_OWNER_USER,
                "skip": offset,
                "limit": limit,
                "include_total": True,
            },
        )
        response.raise_for_status()
        return {
            "users": [self._as_mlpa_user(user) for user in response.json()],
            "total": int(response.headers["otari-total-count"]),
            "limit": limit,
            "offset": offset,
        }

    async def count_users_by_service_type(self) -> dict:
        """Users per service type, counted as the users on each service type's budget.

        Otari counts users per budget, so a user moved to another service type's
        budget (a tester on ai-dev) counts under that one, where LiteLLM counted it
        under the service type in its id.
        """
        budgets = await self._request("GET", "/budgets", params={"limit": _PAGE})
        users_on = {budget["budget_id"]: budget["user_count"] for budget in budgets}
        counts = {
            service_type: users_on.get(config["budget_id"], 0)
            for service_type, config in env.service_type_config.items()
        }
        counts = {st: count for st, count in counts.items() if count}
        return {"service_type_counts": counts, "total_users": sum(counts.values())}

    async def _service_key(self) -> dict[str, Any] | None:
        keys = await self._request("GET", "/keys", params={"limit": _PAGE})
        for key in keys:
            if key.get("key_name") == env.OTARI_OWNER_USER and key.get(
                "is_active", True
            ):
                return key
        return None

    async def _learn_key(self) -> dict[str, Any] | None:
        key = await self._service_key()
        self._key_id = key["id"] if key is not None else None
        return key

    async def provision(self, *, rotate: bool = False) -> str | None:
        """Write MLPA's budgets and its service key to Otari, returning the key's secret when it is known.

        The one writer: run as a deploy step (scripts/otari_provision.py), never
        by the serving replicas, which would overwrite edits made in Otari. Each
        budget is put under MLPA's own budget id, so running it again changes
        nothing. A key's secret is shown once, when it is created or rotated, so an
        existing key's secret is only returned when ``rotate`` is set.
        """
        for service_type, config in env.service_type_config.items():
            await self._request(
                "PUT",
                f"/budgets/{config['budget_id']}",
                json={
                    "name": config["budget_id"],
                    "max_budget": config["max_budget"],
                    **budget_period(config["budget_duration"]),
                    # Per user per minute; tokens are counted on what requests used, as LiteLLM does.
                    "rpm_limit": config["rpm_limit"],
                    "tpm_limit": config["tpm_limit"],
                },
            )
            logger.info(
                f"Budget provisioned: budget_id={config['budget_id']}, service_type={service_type}"
            )

        listed = budget_ids()
        # MLPA names a budget on every request, so the default only catches a
        # request that somehow names none: it is the first service type's.
        assignment = {"end_user_budget_ids": listed, "end_user_budget_id": listed[0]}
        secret: str | None = None
        key = await self._learn_key()
        if key is None:
            created = await self._request(
                "POST",
                "/keys",
                json={
                    "key_name": env.OTARI_OWNER_USER,
                    "user_id": env.OTARI_OWNER_USER,
                    "is_service_key": True,
                    **assignment,
                },
            )
            self._key_id = key_id = created["id"]
            secret = created["key"]
        else:
            key_id = key["id"]
            if (
                key.get("end_user_budget_ids") != listed
                or key.get("end_user_budget_id") != listed[0]
            ):
                await self._request("PATCH", f"/keys/{key_id}", json=assignment)
            if rotate:
                rotated = await self._request("POST", f"/keys/{key_id}/rotate")
                secret = rotated["key"]
        if env.OTARI_GLOBAL_MAX_BUDGET is not None:
            await self._provision_global_budget(key_id)
        return secret

    async def _provision_global_budget(self, key_id: str) -> None:
        """Cap MLPA's service key, and so every end user behind it, at the global budget."""
        budget_id = env.OTARI_GLOBAL_BUDGET_ID
        await self._request(
            "PUT",
            f"/budgets/{budget_id}",
            json={
                "name": budget_id,
                "max_budget": env.OTARI_GLOBAL_MAX_BUDGET,
                "reset_alignment": "calendar_day",
            },
        )
        ceilings = await self._request(
            "GET",
            "/scoped-budgets",
            params={"scope_type": "api_token", "scope_id": key_id},
        )
        if not any(c["budget_id"] == budget_id for c in ceilings):
            await self._request(
                "POST",
                "/scoped-budgets",
                json={
                    "scope_type": "api_token",
                    "scope_id": key_id,
                    "budget_id": budget_id,
                    "name": "MLPA global budget",
                },
            )
        logger.info(
            f"Global budget provisioned: budget_id={budget_id}, key_id={key_id}"
        )

    async def create_budget(self) -> None:
        """Learn the service key's id, and say what provisioning has not done.

        Called at startup in place of the LiteLLM budget upsert, and read-only:
        scripts/otari_provision.py is what writes budgets and the key.
        """
        if not env.OTARI_MASTER_KEY:
            logger.info(
                "No OTARI_MASTER_KEY: skipping the startup check of MLPA's budgets and key in Otari"
            )
            return
        try:
            key = await self._learn_key()
            budgets = await self._request("GET", "/budgets", params={"limit": _PAGE})
        except Exception as e:
            logger.error(f"Error reading MLPA's budgets and key from Otari: {e}")
            return
        if key is None:
            logger.error(
                f"Otari has no service key named {env.OTARI_OWNER_USER!r}; run scripts/otari_provision.py"
            )
            return
        present = {budget["budget_id"] for budget in budgets}
        assignable = set(key.get("end_user_budget_ids") or [])
        for service_type, config in env.service_type_config.items():
            budget_id = config["budget_id"]
            if budget_id not in present:
                logger.error(
                    f"Otari has no budget {budget_id!r} (service type {service_type}); "
                    "run scripts/otari_provision.py"
                )
            elif budget_id not in assignable:
                logger.error(
                    f"MLPA's Otari key may not assign budget {budget_id!r} (service type {service_type}); "
                    "run scripts/otari_provision.py"
                )

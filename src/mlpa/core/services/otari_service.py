"""MLPA's users, budgets and limits kept in Otari, through Otari's management API.

A drop-in for ``LiteLLMPGService`` when ``GATEWAY_BACKEND=otari``: the same
method names and return shapes, so the user router, the signup cap and startup
call it unchanged.

How MLPA's model maps onto Otari's:

- Each service type has an Otari owner user (``mlpa-<service type>``) holding one
  service key. A request's ``user`` ("<identity>:<service type>") names an end user
  of that owner, which Otari creates on first use with the key's end-user budget.
- Each service type's budget is an Otari budget with MLPA's budget id as its name.
- Each service type's per-user RPM and TPM are that budget's ``rpm_limit`` and
  ``tpm_limit``, so moving a user to another budget moves its limits too.
"""

import re
from collections import OrderedDict
from typing import Any

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


def owner_of(service_type: str) -> str:
    return f"{env.OTARI_OWNER_PREFIX}{service_type}"


def key_name_of(service_type: str) -> str:
    return f"{env.OTARI_OWNER_PREFIX}{service_type}"


class OtariService:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._budget_ids: dict[str, str] = {}  # MLPA budget id -> Otari budget id
        self._budget_names: dict[str, str] = {}  # Otari budget id -> MLPA budget id
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

    def _as_mlpa_user(self, user: dict[str, Any]) -> dict[str, Any]:
        """An Otari end user in the shape MLPA read from LiteLLM_EndUserTable."""
        budget_id = user.get("budget_id")
        return {
            "user_id": user.get("external_id"),
            "otari_user_id": user.get("user_id"),
            "budget_id": self._budget_names.get(budget_id, budget_id)
            if budget_id
            else None,
            "blocked": bool(user.get("blocked")),
            "spend": user.get("spend", 0.0),
            "budget_started_at": user.get("budget_started_at"),
            "next_budget_reset_at": user.get("next_budget_reset_at"),
            "created_at": user.get("created_at"),
        }

    async def _find(self, user_id: str) -> dict[str, Any] | None:
        _base, _sep, service_type = user_id.partition(":")
        if not service_type:
            return None
        users = await self._request(
            "GET",
            "/users",
            params={
                "parent_user_id": owner_of(service_type),
                "external_id": user_id,
                "limit": 1,
            },
        )
        return users[0] if users else None

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        user = await self._find(user_id)
        if user is None:
            return None
        self.remember(user_id)
        return self._as_mlpa_user(user)

    async def update_user_budget(self, user_id: str, budget_id: str) -> dict:
        user = await self._find(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found.")
        otari_budget_id = self._budget_ids.get(budget_id)
        if otari_budget_id is None:
            raise HTTPException(
                status_code=500, detail={"error": "Error updating user budget"}
            )
        updated = await self._request(
            "PATCH", f"/users/{user['user_id']}", json={"budget_id": otari_budget_id}
        )
        logger.info(f"User {user_id} budget updated to {budget_id} successfully.")
        return self._as_mlpa_user(updated)

    async def block_user(self, user_id: str, blocked: bool = True) -> dict:
        user = await self._find(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found.")
        updated = await self._request(
            "PATCH", f"/users/{user['user_id']}", json={"blocked": blocked}
        )
        logger.info(
            f"User {user_id} {'blocked' if blocked else 'unblocked'} successfully."
        )
        return self._as_mlpa_user(updated)

    async def _count(self, service_type: str) -> int:
        response = await self.client.get(
            "/users",
            params={
                "parent_user_id": owner_of(service_type),
                "limit": 1,
                "include_total": True,
            },
        )
        response.raise_for_status()
        return int(response.headers["otari-total-count"])

    async def list_users(self, limit: int = 50, offset: int = 0) -> dict:
        """Users across every service type, in service-type order."""
        counts = {st: await self._count(st) for st in env.valid_service_types}
        users: list[dict[str, Any]] = []
        skip = offset
        for service_type, count in counts.items():
            if len(users) >= limit:
                break
            if skip >= count:
                skip -= count
                continue
            page = await self._request(
                "GET",
                "/users",
                params={
                    "parent_user_id": owner_of(service_type),
                    "skip": skip,
                    "limit": limit - len(users),
                },
            )
            users.extend(self._as_mlpa_user(user) for user in page)
            skip = 0
        return {
            "users": users,
            "total": sum(counts.values()),
            "limit": limit,
            "offset": offset,
        }

    async def count_users_by_service_type(self) -> dict:
        counts = {st: await self._count(st) for st in env.valid_service_types}
        counts = {st: count for st, count in counts.items() if count}
        return {"service_type_counts": counts, "total_users": sum(counts.values())}

    async def list_managed_base_identities(
        self, managed_service_types: list[str]
    ) -> list[str]:
        identities: set[str] = set()
        for service_type in managed_service_types:
            skip = 0
            while True:
                page = await self._request(
                    "GET",
                    "/users",
                    params={
                        "parent_user_id": owner_of(service_type),
                        "skip": skip,
                        "limit": _PAGE,
                    },
                )
                identities.update(
                    (user.get("external_id") or "").partition(":")[0] for user in page
                )
                if len(page) < _PAGE:
                    break
                skip += _PAGE
        identities.discard("")
        return sorted(identities)

    async def has_managed_user_rows(
        self, base_identity: str, managed_service_types: list[str]
    ) -> bool:
        for service_type in managed_service_types:
            if await self._find(f"{base_identity}:{service_type}") is not None:
                return True
        return False

    # Budgets and keys

    async def _budgets_by_name(self) -> dict[str, list[dict[str, Any]]]:
        by_name: dict[str, list[dict[str, Any]]] = {}
        for budget in await self._request("GET", "/budgets", params={"limit": 1000}):
            by_name.setdefault(budget.get("name"), []).append(budget)
        return by_name

    def _learn_budget(self, name: str, budget_id: str) -> None:
        self._budget_ids[name] = budget_id
        self._budget_names[budget_id] = name

    async def _keys_by_name(self) -> dict[str, dict[str, Any]]:
        keys = await self._request("GET", "/keys", params={"limit": 1000})
        return {key["key_name"]: key for key in keys if key.get("is_active", True)}

    async def provision(self, *, rotate: bool = False) -> dict[str, str]:
        """Write MLPA's budgets and service keys to Otari, returning the key secrets it could read.

        The one writer: run as a deploy step (scripts/otari_provision.py), never
        by the serving replicas, which would race each other into duplicate
        budgets and overwrite edits made in Otari. A key's secret is shown once,
        when it is created or rotated, so an existing key's secret is only
        returned when ``rotate`` is set.
        """
        budgets = await self._budgets_by_name()
        for name, found in budgets.items():
            if name and len(found) > 1:
                raise RuntimeError(
                    f"Otari has {len(found)} budgets named {name!r}; remove all but one"
                )
        for service_type, config in env.service_type_config.items():
            name = config["budget_id"]
            body = {
                "name": name,
                "max_budget": config["max_budget"],
                "budget_duration_sec": duration_seconds(config["budget_duration"]),
                # Per user per minute; tokens are counted on what requests used, as LiteLLM does.
                "rpm_limit": config["rpm_limit"],
                "tpm_limit": config["tpm_limit"],
            }
            if name in budgets:
                budget = await self._request(
                    "PATCH", f"/budgets/{budgets[name][0]['budget_id']}", json=body
                )
            else:
                budget = await self._request("POST", "/budgets", json=body)
            self._learn_budget(name, budget["budget_id"])
            logger.info(
                f"Budget provisioned: budget_id={name}, service_type={service_type}"
            )

        keys = await self._keys_by_name()
        secrets: dict[str, str] = {}
        for service_type, config in env.service_type_config.items():
            budget_id = self._budget_ids[config["budget_id"]]
            key = keys.get(key_name_of(service_type))
            if key is None:
                created = await self._request(
                    "POST",
                    "/keys",
                    json={
                        "key_name": key_name_of(service_type),
                        "user_id": owner_of(service_type),
                        "is_service_key": True,
                        "end_user_budget_id": budget_id,
                    },
                )
                secrets[service_type] = created["key"]
                continue
            if key.get("end_user_budget_id") != budget_id:
                await self._request(
                    "PATCH",
                    f"/keys/{key['id']}",
                    json={"end_user_budget_id": budget_id},
                )
            if rotate:
                rotated = await self._request("POST", f"/keys/{key['id']}/rotate")
                secrets[service_type] = rotated["key"]
        return secrets

    async def create_budget(self) -> None:
        """Learn the Otari ids of MLPA's budgets, and say what provisioning has not done.

        Called at startup in place of the LiteLLM budget upsert, and read-only:
        scripts/otari_provision.py is what writes budgets and keys.
        """
        try:
            budgets = await self._budgets_by_name()
            keys = await self._keys_by_name()
        except Exception as e:
            logger.error(f"Error reading budgets from Otari: {e}")
            return
        for service_type, config in env.service_type_config.items():
            name = config["budget_id"]
            found = budgets.get(name, [])
            if len(found) != 1:
                logger.error(
                    f"Otari has {len(found)} budgets named {name!r} (service type {service_type}), "
                    "expected 1; run scripts/otari_provision.py"
                )
            if found:
                self._learn_budget(name, found[0]["budget_id"])
            if key_name_of(service_type) not in keys:
                logger.error(
                    f"Otari has no service key for service type {service_type}; run scripts/otari_provision.py"
                )

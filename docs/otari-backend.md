# Otari backend (pilot)

`GATEWAY_BACKEND=otari` sends MLPA's inference to [Otari](https://github.com/mozilla-ai/otari) instead of LiteLLM.
Everything Firefox-specific stays in MLPA: auth, service types, the signup cap and error codes.

| Setting | Meaning |
|---|---|
| `GATEWAY_BACKEND` | `litellm` (default) or `otari` |
| `OTARI_API_BASE` | Otari's base URL, without `/api/v1` |
| `OTARI_MASTER_KEY` | Otari master key, for MLPA's user and budget management calls |
| `OTARI_SERVICE_KEYS` | JSON object, service type to Otari service key, written by `scripts/otari_provision.py` |
| `OTARI_OWNER_PREFIX` | Prefix of the Otari user that owns each service type's key and end users (`mlpa-`) |

How MLPA's model maps onto Otari:

- **Users.** Each service type has an Otari owner user (`mlpa-<service type>`) holding one service
  key. The `user` MLPA sends (`<identity>:<service type>`) names an end user of that owner, and Otari
  creates it on first use with the key's end-user budget. `/customer/new` and the direct SQL into
  LiteLLM's tables are gone: `OtariService` answers the same calls through Otari's users API.
- **Budgets and limits.** Each service type's budget is an Otari budget named after its `budget_id`.
  Its per-user RPM and TPM are a `rate_limits` rule, `per: user`, narrowed to that service type's key,
  with `tpm_admission: used`, so a request is limited by the tokens it used, as LiteLLM does.
  `create_budget()` at startup brings budgets, key budgets and rules in line with the config.
- **Errors.** Errors are mapped from Otari's `Otari-Error-Code` header, not from message text.
  `budget_exceeded` maps to 1, or to 10 when `Otari-Budget-Scope` is not `user`. `rate_limited` maps
  to 2, `upstream_rate_limited` to 5, `invalid_model` to 8, and `user_blocked` to
  403 `User is blocked.`.

Provision once per environment; this writes `OTARI_SERVICE_KEYS` to the env file:

```bash
GATEWAY_BACKEND=otari OTARI_API_BASE=... OTARI_MASTER_KEY=... \
  uv run python scripts/otari_provision.py [--rotate] [--env-file .env]
```

A local stack (MLPA, Otari, fake providers) with end-to-end checks lives in Otari's pilot branch,
under `pilot/`.

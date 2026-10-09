# Otari backend (pilot)

`GATEWAY_BACKEND=otari` sends MLPA's inference to [Otari](https://github.com/mozilla-ai/otari) instead of LiteLLM.
Everything Firefox-specific stays in MLPA: auth, service types, the signup cap and error codes.

| Setting | Meaning |
|---|---|
| `GATEWAY_BACKEND` | `litellm` (default) or `otari` |
| `OTARI_API_BASE` | Otari's base URL, without `/api/v1` |
| `OTARI_MASTER_KEY` | Otari master key, for MLPA's user and budget management calls |
| `OTARI_SERVICE_KEY` | MLPA's Otari service key, written by `scripts/otari_provision.py` |
| `OTARI_OWNER_USER` | The Otari user that owns the key and every end user, and the key's name (`mlpa`) |

How MLPA's model maps onto Otari:

- **One key.** MLPA holds one Otari service key, owned by the Otari user `mlpa`, for every service
  type. The `user` MLPA sends (`<identity>:<service type>`) names an end user of that owner, and each
  request sends `Otari-End-User-Budget` with the service type's `budget_id`, which is the budget Otari
  creates a new end user on. An existing end user keeps its budget, so moving a tester to `ai-dev`
  sticks. `/customer/new` and the direct SQL into LiteLLM's tables are gone.
- **Users.** `OtariService` reads, blocks and moves a user with one call to
  `/api/v1/keys/{key_id}/end-users/{identity:service_type}`, and lists them from `/api/v1/users`.
  Counts per service type are the `user_count` of each service type's budget, so a user moved to
  another service type's budget counts under that one.
- **Budgets and limits.** Each service type's budget is an Otari budget under MLPA's own `budget_id`
  (`PUT /api/v1/budgets/{budget_id}`), carrying the dollar cap and period plus the per-user
  `rpm_limit` and `tpm_limit` (tokens counted on what requests used, as LiteLLM counts them). The key
  lists all of them as the budgets it may assign. Moving a user to another budget moves all of it.
- **Provisioning.** `scripts/otari_provision.py` is the only writer, run as a deploy step; running it
  again changes nothing. MLPA replicas only read at startup, to learn the key's id and log a budget
  that is missing or that the key may not assign.
- **Errors.** Errors are mapped from Otari's `Otari-Error-Code` header, not from message text.
  The code comes from the header or the body's `code`. `budget_exceeded` maps to 1, or to 10 when
  `Otari-Budget-Scope` is not `user`. `rate_limited` maps to 2, `upstream_rate_limited` to 5,
  `context_length_exceeded` to 3, `invalid_model` to 8, and `user_blocked` to 403
  `User is blocked.`. `end_user_budget_not_allowed` means the key does not list a service type's
  budget, which provisioning fixes. A stream Otari ends with a coded error event becomes `data: {"error": N}`.

Provision once per environment; this writes `OTARI_SERVICE_KEY` to the env file:

```bash
GATEWAY_BACKEND=otari OTARI_API_BASE=... OTARI_MASTER_KEY=... \
  uv run python scripts/otari_provision.py [--rotate] [--env-file .env]
```

A local stack (Firefox, MLPA, Otari on its `main` branch, fake providers) with end-to-end checks
lives in [mozilla-ai/otari-firefox-pilot](https://github.com/mozilla-ai/otari-firefox-pilot).

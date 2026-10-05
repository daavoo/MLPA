"""
Provision MLPA in Otari, the counterpart of create-and-set-virtual-key.py.

Creates or updates, for each service type, the budget (with its per-user RPM
and TPM), the owner user and the service key, then writes the keys' secrets to the env file as
OTARI_SERVICE_KEYS. A key's secret is only shown when it is created, so a key
that already exists is rotated only with --rotate.

    GATEWAY_BACKEND=otari OTARI_MASTER_KEY=... uv run python scripts/otari_provision.py [--rotate]
"""

import argparse
import asyncio
import json
import os

from mlpa.core.config import env
from mlpa.core.services.otari_service import OtariService


def _write_env(env_file: str, name: str, value: str) -> None:
    lines: list[str] = []
    if os.path.exists(env_file):
        with open(env_file) as f:
            lines = [line for line in f if not line.startswith(f"{name}=")]
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    # Single-quoted, so a shell sourcing the file keeps the JSON's own quotes.
    lines.append(f"{name}='{value}'\n")
    with open(env_file, "w") as f:
        f.writelines(lines)


async def main(rotate: bool, env_file: str) -> None:
    service = OtariService()
    await service.connect()
    try:
        secrets = await service.provision(rotate=rotate)
    finally:
        await service.disconnect()
    keys = {**env.OTARI_SERVICE_KEYS, **secrets}
    missing = sorted(set(env.service_type_config) - set(keys))
    if secrets:
        _write_env(
            env_file, "OTARI_SERVICE_KEYS", json.dumps(keys, separators=(",", ":"))
        )
        print(
            f"Wrote {len(secrets)} service key(s) to {env_file}: {', '.join(sorted(secrets))}"
        )
    if missing:
        print(f"No secret known for: {', '.join(missing)}. Run again with --rotate.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="rotate existing keys to learn their secrets",
    )
    parser.add_argument(
        "--env-file", default=".env", help="file to write OTARI_SERVICE_KEYS to"
    )
    args = parser.parse_args()
    asyncio.run(main(args.rotate, args.env_file))

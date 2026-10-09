"""
Provision MLPA in Otari, the counterpart of create-and-set-virtual-key.py.

Puts each service type's budget (with its per-user RPM and TPM) under MLPA's own
budget id, and creates or updates MLPA's one service key, listing every budget
it may start an end user on. Then writes the key's secret to the env file as
OTARI_SERVICE_KEY. A key's secret is only shown when it is created, so a key
that already exists is rotated only with --rotate.

    GATEWAY_BACKEND=otari OTARI_MASTER_KEY=... uv run python scripts/otari_provision.py [--rotate]
"""

import argparse
import asyncio
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
    lines.append(f"{name}={value}\n")
    with open(env_file, "w") as f:
        f.writelines(lines)


async def main(rotate: bool, env_file: str) -> None:
    service = OtariService()
    await service.connect()
    try:
        secret = await service.provision(rotate=rotate)
    finally:
        await service.disconnect()
    if secret is not None:
        _write_env(env_file, "OTARI_SERVICE_KEY", secret)
        print(f"Wrote the service key to {env_file}")
    elif not env.OTARI_SERVICE_KEY:
        print("The service key's secret is not known. Run again with --rotate.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="rotate the existing key to learn its secret",
    )
    parser.add_argument(
        "--env-file", default=".env", help="file to write OTARI_SERVICE_KEY to"
    )
    args = parser.parse_args()
    asyncio.run(main(args.rotate, args.env_file))

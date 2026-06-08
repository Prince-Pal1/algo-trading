#!/usr/bin/env python3
"""Non-interactive refresh of the cTrader OAuth access token.

Reads CTRADER_REFRESH_TOKEN + CTRADER_CLIENT_ID + CTRADER_CLIENT_SECRET
from .env, POSTs to Spotware's token endpoint with
`grant_type=refresh_token`, and writes the rotated access+refresh
tokens back to .env. No browser interaction.

Designed for two callers:
  1. Operator one-shot:   `python3 scripts/ctrader_refresh_token.py`
  2. icmarkets_feed.py auto-refresh on CH_ACCESS_TOKEN_INVALID
     (imports `refresh_tokens()` directly).

Spotware rotates the refresh_token on every exchange — the new value
MUST be persisted or the next refresh will 400.

Exit codes: 0 ok, 1 missing creds, 2 HTTP failure, 3 response missing
access token.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: requests is required. Run `pip install requests`.")
    sys.exit(1)

TOKEN_URL = "https://openapi.ctrader.com/apps/token"


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def _write_env_tokens(path: Path, access_token: str, refresh_token: str) -> None:
    lines: list[str] = []
    seen_access = False
    seen_refresh = False
    if path.exists():
        for raw in path.read_text().splitlines():
            if raw.startswith("CTRADER_ACCESS_TOKEN="):
                lines.append(f"CTRADER_ACCESS_TOKEN={access_token}")
                seen_access = True
            elif raw.startswith("CTRADER_REFRESH_TOKEN="):
                lines.append(f"CTRADER_REFRESH_TOKEN={refresh_token}")
                seen_refresh = True
            else:
                lines.append(raw)
    if not seen_access:
        lines.append(f"CTRADER_ACCESS_TOKEN={access_token}")
    if not seen_refresh:
        lines.append(f"CTRADER_REFRESH_TOKEN={refresh_token}")
    path.write_text("\n".join(lines) + "\n")


def refresh_tokens(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    *,
    timeout_s: float = 30.0,
) -> tuple[str, str]:
    """Exchange a refresh_token for a new access+refresh pair.

    Returns (access_token, refresh_token). Raises RuntimeError on failure.
    Spotware rotates the refresh_token — the returned value MUST be
    persisted by the caller.
    """
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=timeout_s,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"token refresh failed: HTTP {response.status_code} — {response.text[:300]}"
        )
    data = response.json()
    new_access = data.get("accessToken") or data.get("access_token", "")
    new_refresh = data.get("refreshToken") or data.get("refresh_token", "")
    if not new_access:
        raise RuntimeError(f"refresh response missing access token: {data}")
    if not new_refresh:
        # If Spotware ever stops rotating, fall back to the existing
        # refresh token so the caller still has something to persist.
        new_refresh = refresh_token
    return new_access, new_refresh


def main() -> int:
    env_path = Path(".env")
    env_values = _load_env_file(env_path)

    client_id = os.getenv("CTRADER_CLIENT_ID") or env_values.get("CTRADER_CLIENT_ID", "")
    client_secret = os.getenv("CTRADER_CLIENT_SECRET") or env_values.get("CTRADER_CLIENT_SECRET", "")
    refresh_token = os.getenv("CTRADER_REFRESH_TOKEN") or env_values.get("CTRADER_REFRESH_TOKEN", "")

    if not (client_id and client_secret and refresh_token):
        print("ERROR: CTRADER_CLIENT_ID, CTRADER_CLIENT_SECRET, and CTRADER_REFRESH_TOKEN must be set.")
        print(f"  client_id     : {'OK' if client_id else 'MISSING'}")
        print(f"  client_secret : {'OK' if client_secret else 'MISSING'}")
        print(f"  refresh_token : {'OK' if refresh_token else 'MISSING'}")
        return 1

    print(f"Refreshing access token via {TOKEN_URL} …")
    try:
        new_access, new_refresh = refresh_tokens(client_id, client_secret, refresh_token)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 2

    _write_env_tokens(env_path, new_access, new_refresh)
    print(f"✓ Tokens rotated and written to {env_path.absolute()}")
    print(f"  access_token  : {new_access[:16]}...{new_access[-8:]}")
    print(f"  refresh_token : {new_refresh[:16]}...{new_refresh[-8:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Configuration every server shares: caller authentication and where to listen.

Secrets come from the environment (systemd EnvironmentFile in production, a
gitignored .env locally). Nothing secret is ever defaulted — a missing value is
a startup failure with a message naming what to set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class KitConfig:
    """What the kit needs from any server's config.

    Servers subclass this and add their own fields. Dataclass inheritance puts
    these first in `__init__`, so every construction uses keywords.
    """

    # Caller authentication. Absent in local/stdio use, required to serve HTTP.
    github_client_id: str | None
    github_client_secret: str | None
    base_url: str | None
    allowed_gh_id: str | None

    host: str
    port: int

    @property
    def auth_configured(self) -> bool:
        return bool(
            self.github_client_id
            and self.github_client_secret
            and self.base_url
            and self.allowed_gh_id
        )


def kit_env(port: int) -> dict[str, Any]:
    """KitConfig's fields from the environment; `port` is the server's default."""
    return {
        "github_client_id": os.environ.get("GITHUB_CLIENT_ID") or None,
        "github_client_secret": os.environ.get("GITHUB_CLIENT_SECRET") or None,
        "base_url": os.environ.get("BASE_URL") or None,
        "allowed_gh_id": os.environ.get("ALLOWED_GH_ID") or None,
        "host": os.environ.get("HOST", "127.0.0.1"),
        "port": int(os.environ.get("PORT", str(port))),
    }

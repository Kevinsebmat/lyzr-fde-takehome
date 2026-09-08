"""Startup: load `.env`, then decide live-vs-mock explicitly.

Every CLI and the FastAPI app call `bootstrap()` first. The point is that a
reviewer who clones this repo and runs a command gets a working demo and a
one-line explanation of which mode they're in — not an SDK stack trace about
authentication headers.

Credential resolution deliberately mirrors the SDK's: an unset
`ANTHROPIC_API_KEY` does *not* mean there are no credentials, because an
`ant auth login` profile counts too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MOCK = "mock"
LIVE = "live"


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader. Existing environment always wins."""
    p = Path(path)
    if not p.exists():
        # Also look at the repo root, so a project CLI run from its own
        # directory still picks up the shared .env.
        p = Path(__file__).resolve().parent.parent.parent / ".env"
        if not p.exists():
            return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


def credentials_available() -> bool:
    """True if the SDK will find a credential.

    Checks the same sources the SDK does, in order, so we don't tell someone
    with a valid OAuth profile that they have no credentials.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    profiles = Path.home() / ".config" / "anthropic"
    return profiles.is_dir() and any(profiles.glob("*.json"))


def bootstrap(*, quiet: bool = False) -> str:
    """Return the mode this process will run in, configuring it if needed.

    Precedence:
      1. An explicit `LLM_PROVIDER=mock` is honoured, always.
      2. Otherwise, live if a credential is resolvable.
      3. Otherwise, fall back to mock and say so — a demo that runs beats a
         demo that raises, as long as it is not pretending to be live.
    """
    load_dotenv()

    if os.environ.get("LLM_PROVIDER", "").lower() == MOCK:
        return MOCK

    if credentials_available():
        return LIVE

    os.environ["LLM_PROVIDER"] = MOCK
    if not quiet:
        print(
            "no Anthropic credential found — running in mock mode (offline, free, "
            "deterministic).\nSet ANTHROPIC_API_KEY (or run `ant auth login`) for "
            "real model calls.\n",
            file=sys.stderr,
        )
    return MOCK

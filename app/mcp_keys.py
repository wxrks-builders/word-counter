"""Personal keys for the MCP server — how Claude, ChatGPT and other assistants
count words as one of our users.

A key is a password: it's shown once, when it's made, and only its SHA-256 is
kept. It travels either as `Authorization: Bearer <key>` (Claude Code, Cursor,
anything that can send a header) or inside the personal connector URL
`/mcp/<key>`, for apps that only take a URL (ChatGPT, Claude's web and desktop
connectors). Revoking it stops both at once.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid

from app import db
from app.models import User

PREFIX = "wck_"
# Keys a user can have at once — one per app or machine is plenty.
MAX_KEYS = 10


def new_token() -> str:
    return PREFIX + secrets.token_urlsafe(24)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def looks_like_key(token: str | None) -> bool:
    return bool(token) and token.startswith(PREFIX) and 20 <= len(token) <= 80


async def create(user: User, name: str) -> tuple[str, dict]:
    """A new key: the token (to show once) and its stored row."""
    token = new_token()
    row = await db.create_api_key(user.id, uuid.uuid4().hex[:12], (name or "").strip()[:60] or "My assistant",
                                  hash_token(token), token[-4:])
    return token, row


async def user_for(token: str | None) -> User | None:
    if not looks_like_key(token):
        return None
    return await db.user_for_api_key(hash_token(token))

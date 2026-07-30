"""Telegram Mini App ``initData`` verification.

A Mini App's ``initData`` is the only thing identifying the caller, and it arrives
from the client — so it must be verified server-side on every request. Trusting the
``user.id`` field without checking the HMAC means anyone can claim to be anyone.

Algorithm (per Telegram's Mini Apps documentation):
  secret = HMAC_SHA256(key="WebAppData", message=bot_token)
  expected = HMAC_SHA256(key=secret, message=data_check_string)
where ``data_check_string`` is every field except ``hash``, sorted by key, joined
with newlines as ``key=value``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

# initData older than this is rejected even when the signature is valid, so a
# captured payload cannot be replayed indefinitely.
MAX_AGE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class TelegramUser:
    telegram_id: int
    username: str = ""
    first_name: str = ""


class InitDataError(Exception):
    """Raised when initData is missing, malformed, expired, or forged."""


def verify_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int = MAX_AGE_SECONDS,
    now: float | None = None,
) -> TelegramUser:
    """Verify a Mini App initData string and return the authenticated user."""
    if not init_data:
        raise InitDataError("initData is missing")
    if not bot_token:
        raise InitDataError("server has no bot token configured; cannot verify initData")

    # keep_blank_values matters: dropping empty fields changes the check string
    # and would make otherwise-valid payloads fail.
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = fields.pop("hash", "")
    if not received_hash:
        raise InitDataError("initData has no hash")

    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()

    # Constant-time: a plain == leaks how much of the hash matched.
    if not hmac.compare_digest(expected, received_hash):
        raise InitDataError("initData signature is invalid")

    auth_date = fields.get("auth_date", "")
    try:
        issued_at = int(auth_date)
    except ValueError as exc:
        raise InitDataError("initData has no usable auth_date") from exc

    current = now if now is not None else time.time()
    if current - issued_at > max_age_seconds:
        raise InitDataError("initData has expired")

    try:
        user_payload = json.loads(fields.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise InitDataError("initData user field is not valid JSON") from exc

    telegram_id = user_payload.get("id")
    if not isinstance(telegram_id, int):
        raise InitDataError("initData contains no user id")

    return TelegramUser(
        telegram_id=telegram_id,
        username=str(user_payload.get("username", "") or ""),
        first_name=str(user_payload.get("first_name", "") or ""),
    )

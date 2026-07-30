"""Mini App initData verification.

This is the application's only authentication boundary, so it gets adversarial
tests rather than a happy-path smoke test.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from giftpulse.api.auth import InitDataError, verify_init_data

BOT_TOKEN = "123456:TEST-TOKEN-NOT-REAL"


def make_init_data(
    telegram_id: int = 42,
    username: str = "trader",
    auth_date: int | None = None,
    token: str = BOT_TOKEN,
    tamper: bool = False,
) -> str:
    fields = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAEtest",
        "user": json.dumps({"id": telegram_id, "username": username, "first_name": "T"}),
    }
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()

    if tamper:
        fields["user"] = json.dumps({"id": 999, "username": "attacker", "first_name": "A"})

    return urlencode({**fields, "hash": signature})


class TestVerifyInitData:
    def test_accepts_a_correctly_signed_payload(self):
        user = verify_init_data(make_init_data(telegram_id=42), BOT_TOKEN)
        assert user.telegram_id == 42
        assert user.username == "trader"

    def test_rejects_a_tampered_user_id(self):
        # The core attack: keep a valid signature, swap in someone else's id.
        with pytest.raises(InitDataError, match="signature"):
            verify_init_data(make_init_data(tamper=True), BOT_TOKEN)

    def test_rejects_a_payload_signed_with_another_token(self):
        forged = make_init_data(token="999999:SOMEONE-ELSES-BOT")
        with pytest.raises(InitDataError, match="signature"):
            verify_init_data(forged, BOT_TOKEN)

    def test_rejects_expired_payloads(self):
        stale = make_init_data(auth_date=int(time.time()) - 60 * 60 * 48)
        with pytest.raises(InitDataError, match="expired"):
            verify_init_data(stale, BOT_TOKEN)

    def test_accepts_a_payload_inside_the_age_window(self):
        recent = make_init_data(auth_date=int(time.time()) - 60)
        assert verify_init_data(recent, BOT_TOKEN).telegram_id == 42

    def test_rejects_missing_input(self):
        with pytest.raises(InitDataError, match="missing"):
            verify_init_data("", BOT_TOKEN)

    def test_rejects_a_payload_with_no_hash(self):
        with pytest.raises(InitDataError, match="no hash"):
            verify_init_data("auth_date=1&user=%7B%7D", BOT_TOKEN)

    def test_refuses_to_verify_when_the_server_has_no_token(self):
        # Fail closed: without a token there is nothing to verify against, and
        # treating that as "everyone is authenticated" would be catastrophic.
        with pytest.raises(InitDataError, match="bot token"):
            verify_init_data(make_init_data(), "")

    def test_rejects_a_signed_payload_carrying_no_user(self):
        fields = {"auth_date": str(int(time.time())), "query_id": "AAEtest"}
        check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        signature = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()

        with pytest.raises(InitDataError, match="user id"):
            verify_init_data(urlencode({**fields, "hash": signature}), BOT_TOKEN)

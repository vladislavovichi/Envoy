import hashlib
import hmac
import json
from datetime import timedelta
from urllib.parse import quote

import pytest

from envoy.app.miniapp import InitDataError, sign_init_data, validate_init_data
from tests.support.sessions import T0

TOKEN = "123456:TEST-token"
OTHER_TOKEN = "654321:OTHER-token"


def fields(user_id: int = 1, auth=T0) -> dict[str, str]:
    return {
        "auth_date": str(int(auth.timestamp())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps({"id": user_id, "first_name": "Анна", "language_code": "ru"}),
    }


def documented_init_data(values: dict[str, str], token: str) -> str:
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    encoded = "&".join(f"{key}={quote(value, safe='')}" for key, value in values.items())
    return f"{encoded}&hash={digest}"


def test_valid_init_data_matches_documented_algorithm() -> None:
    raw = documented_init_data(fields(), TOKEN)
    user = validate_init_data(raw, TOKEN, T0 + timedelta(minutes=5))
    assert user.id == 1
    assert validate_init_data(sign_init_data(fields(7), TOKEN), TOKEN, T0).id == 7


@pytest.mark.parametrize(
    ("raw", "now", "reason"),
    [
        (documented_init_data(fields(), TOKEN), T0 + timedelta(hours=1, seconds=1), "expired"),
        (documented_init_data(fields(), TOKEN), T0 - timedelta(minutes=5), "expired"),
        (documented_init_data(fields(), OTHER_TOKEN), T0, "bad_hash"),
        (documented_init_data(fields(), TOKEN).replace("user=", "user=x"), T0, "bad_hash"),
        (documented_init_data(fields(), TOKEN).rsplit("&hash=", 1)[0], T0, "missing_hash"),
        (documented_init_data(fields(), TOKEN) + "&auth_date=1", T0, "duplicate_field"),
        ("%%%", T0, "malformed"),
    ],
)
def test_invalid_init_data(raw, now, reason) -> None:
    with pytest.raises(InitDataError) as error:
        validate_init_data(raw, TOKEN, now)
    assert error.value.reason == reason


def test_missing_user_is_rejected() -> None:
    values = fields()
    del values["user"]
    with pytest.raises(InitDataError, match="missing_user"):
        validate_init_data(documented_init_data(values, TOKEN), TOKEN, T0)

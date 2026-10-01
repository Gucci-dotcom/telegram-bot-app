import hashlib
import hmac
import json
from datetime import datetime, time, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.auth import validate_init_data
from app.config import parse_slots
from app.db import Database
from app.slots import next_free_slot

MSK = ZoneInfo("Europe/Moscow")


def test_parse_slots():
    assert parse_slots(" 19:00,09:30, 09:30 ,") == [time(9, 30), time(19, 0)]


def test_next_free_slot_skips_past_and_taken():
    slots = [time(9), time(13), time(19)]
    now = datetime(2026, 10, 1, 10, 0, tzinfo=MSK)
    first = next_free_slot(now, slots, MSK, [])
    assert first == datetime(2026, 10, 1, 13, 0, tzinfo=MSK)
    second = next_free_slot(now, slots, MSK, [first])
    assert second == datetime(2026, 10, 1, 19, 0, tzinfo=MSK)
    third = next_free_slot(now, slots, MSK, [first, second])
    assert third == datetime(2026, 10, 2, 9, 0, tzinfo=MSK)
    assert third.tzinfo == timezone.utc


def test_next_free_slot_without_slots():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert next_free_slot(now, [], MSK, []) == now + timedelta(minutes=1)


def sign(fields: dict, token: str) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


def test_validate_init_data():
    token = "123:abc"
    fields = {"auth_date": "1000", "query_id": "q", "user": json.dumps({"id": 42, "first_name": "Ann"})}
    data = sign(fields, token)
    assert validate_init_data(data, token, now=1500)["id"] == 42
    assert validate_init_data(data, "other:token", now=1500) is None
    assert validate_init_data(data, token, max_age=100, now=1500) is None
    assert validate_init_data(data.replace("Ann", "Bob"), token, now=1500) is None
    assert validate_init_data("garbage", token) is None


def test_db_due_and_claim():
    db = Database(":memory:")
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    past = db.create_post("a", [], "scheduled", now - timedelta(minutes=1))
    db.create_post("b", [], "scheduled", now + timedelta(minutes=1))
    db.create_post("c", [], "draft")
    assert [p["id"] for p in db.due_posts(now)] == [past["id"]]
    assert db.claim_for_publishing(past["id"], ("scheduled",))
    assert not db.claim_for_publishing(past["id"], ("scheduled",))  # второй раз не возьмёт
    assert db.stats()["publishing"] == 1

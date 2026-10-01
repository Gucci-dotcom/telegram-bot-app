from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, parse_slots
from app.db import Database
from app.main import create_app

AUTH = {"X-Admin-Token": "secret"}
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakePublisher:
    channel_title = "Мой канал"
    channel_username = "my_channel"

    def __init__(self):
        self.sent, self.deleted, self.fail = [], [], False

    async def load_channel_info(self):
        pass

    def link(self, message_id):
        return f"https://t.me/{self.channel_username}/{message_id}"

    async def publish(self, post):
        if self.fail:
            raise RuntimeError("Bad Request: chat not found")
        self.sent.append(post)
        return [100 + len(self.sent)]

    async def delete(self, ids):
        self.deleted.extend(ids)


@pytest.fixture
def ctx(tmp_path):
    settings = Settings(
        bot_token="1:x", channel_id="@my_channel", admin_token="secret",
        slots=parse_slots("09:00,19:00"), media_dir=tmp_path / "media",
    )
    publisher = FakePublisher()
    app = create_app(settings, db=Database(":memory:"), publisher=publisher, run_background=False)
    with TestClient(app) as client:
        yield client, publisher, app


def test_requires_auth(ctx):
    client, *_ = ctx
    assert client.get("/api/posts").status_code == 401
    assert client.get("/api/posts", headers={"X-Admin-Token": "nope"}).status_code == 401
    assert client.get("/api/posts", headers={"X-Telegram-Init-Data": "hash=bad"}).status_code == 403
    assert client.get("/").status_code == 200


def test_queue_post_with_photo(ctx):
    client, publisher, app = ctx
    res = client.post(
        "/api/posts", headers=AUTH,
        data={"caption": "Привет", "mode": "queue"},
        files=[("files", ("a.png", PNG, "image/png")), ("files", ("b.png", PNG, "image/png"))],
    )
    assert res.status_code == 201, res.text
    post = res.json()
    assert post["status"] == "scheduled"
    assert len(post["media"]) == 2 and post["media"][0]["url"].startswith("/media/")
    assert client.get(post["media"][0]["url"]).content == PNG
    assert datetime.fromisoformat(post["scheduled_at"]) > datetime.now(timezone.utc)
    # следующий пост в очереди занимает другой слот
    second = client.post("/api/posts", headers=AUTH, data={"caption": "2", "mode": "queue"}).json()
    assert second["scheduled_at"] != post["scheduled_at"]
    assert publisher.sent == []
    assert [p["id"] for p in client.get("/api/posts?status=scheduled", headers=AUTH).json()] == [post["id"], second["id"]]


def test_publish_now_and_delete_from_channel(ctx):
    client, publisher, _ = ctx
    post = client.post("/api/posts", headers=AUTH, data={"caption": "Срочно", "mode": "now"}).json()
    assert post["status"] == "published"
    assert post["link"] == "https://t.me/my_channel/101"
    assert client.patch(f"/api/posts/{post['id']}", headers=AUTH, json={"caption": "x"}).status_code == 409
    assert client.get("/api/channel", headers=AUTH).json()["stats"]["published"] == 1
    assert client.delete(f"/api/posts/{post['id']}?from_channel=true", headers=AUTH).status_code == 204
    assert publisher.deleted == [101]


async def test_scheduler_publishes_due_posts(ctx):
    client, publisher, app = ctx
    when = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    post = client.post("/api/posts", headers=AUTH, data={"caption": "Позже", "mode": "at", "scheduled_at": when}).json()
    scheduler = app.state.scheduler
    assert await scheduler.tick() == 0
    assert await scheduler.tick(datetime.now(timezone.utc) + timedelta(hours=2)) == 1
    assert app.state.db.get_post(post["id"])["status"] == "published"
    assert publisher.sent[0]["caption"] == "Позже"


async def test_failed_post_can_be_retried(ctx):
    client, publisher, app = ctx
    publisher.fail = True
    post = client.post("/api/posts", headers=AUTH, data={"caption": "a", "mode": "now"}).json()
    assert post["status"] == "failed" and "chat not found" in post["error"]
    publisher.fail = False
    retried = client.patch(f"/api/posts/{post['id']}", headers=AUTH, json={"status": "scheduled"}).json()
    assert retried["status"] == "scheduled" and retried["error"] is None
    assert await app.state.scheduler.tick() == 1
    assert app.state.db.get_post(post["id"])["status"] == "published"


def test_draft_edit_and_validation(ctx):
    client, *_ = ctx
    assert client.post("/api/posts", headers=AUTH, data={"caption": "  ", "mode": "queue"}).status_code == 422
    assert client.post("/api/posts", headers=AUTH, data={"caption": "a" * 1025, "mode": "queue"},
                       files=[("files", ("a.png", PNG, "image/png"))]).status_code == 422
    assert client.post("/api/posts", headers=AUTH, data={"caption": "a", "mode": "at",
                                                         "scheduled_at": "2026-10-01T10:00"}).status_code == 422
    draft = client.post("/api/posts", headers=AUTH, data={"caption": "черновик", "mode": "draft"}).json()
    assert draft["status"] == "draft" and draft["scheduled_at"] is None
    queued = client.patch(f"/api/posts/{draft['id']}", headers=AUTH, json={"caption": "готово", "status": "scheduled"}).json()
    assert queued["caption"] == "готово" and queued["status"] == "scheduled" and queued["scheduled_at"]
    back = client.patch(f"/api/posts/{draft['id']}", headers=AUTH, json={"status": "draft"}).json()
    assert back["status"] == "draft" and back["scheduled_at"] is None

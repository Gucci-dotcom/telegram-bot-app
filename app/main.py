import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import validate_init_data
from .config import Settings
from .db import Database, utcnow
from .publisher import MAX_MEDIA, Publisher, Scheduler, caption_limit
from .slots import next_free_slot

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
EDITABLE = ("draft", "scheduled", "failed")


class PostPatch(BaseModel):
    caption: str | None = None
    scheduled_at: datetime | None = None
    status: Literal["draft", "scheduled"] | None = None


def parse_when(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, "Неверный формат даты")
    if dt.tzinfo is None:
        raise HTTPException(422, "Дата должна содержать часовой пояс")
    return dt.astimezone(timezone.utc)


def create_app(
    settings: Settings,
    db: Database | None = None,
    publisher: Publisher | None = None,
    run_background: bool = True,
) -> FastAPI:
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    db = db or Database(settings.db_path)
    bot = None
    if publisher is None:
        from aiogram import Bot

        bot = Bot(settings.bot_token)
        publisher = Publisher(bot, settings.channel_id, settings.media_dir)
    scheduler = Scheduler(db, publisher, settings.poll_interval)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # пост, зависший в 'publishing' после падения, мог как уйти, так и не уйти — пусть решает человек
        for post in db.list_posts("publishing", limit=1000):
            db.update_post(post["id"], status="failed", error="Публикация прервана перезапуском, проверьте канал")
        tasks = []
        if run_background:
            await publisher.load_channel_info()
            tasks.append(asyncio.create_task(scheduler.run_forever()))
            if bot is not None:
                from aiogram import Dispatcher

                from .bot import build_router, setup_menu_button

                dp = Dispatcher()
                dp.include_router(build_router(settings, db, scheduler))
                with contextlib.suppress(Exception):
                    await setup_menu_button(bot, settings)
                tasks.append(asyncio.create_task(dp.start_polling(bot, handle_signals=False)))
        yield
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if bot is not None:
            await bot.session.close()

    app = FastAPI(title="Telegram Channel Feed", lifespan=lifespan)
    app.state.db, app.state.scheduler, app.state.publisher = db, scheduler, publisher

    def require_admin(
        x_telegram_init_data: str | None = Header(default=None),
        x_admin_token: str | None = Header(default=None),
    ) -> None:
        if x_telegram_init_data:
            user = validate_init_data(x_telegram_init_data, settings.bot_token)
            if user and user.get("id") in settings.admin_ids:
                return
            raise HTTPException(403, "Нет доступа: ваш Telegram id не в ADMIN_IDS")
        if settings.admin_token and x_admin_token == settings.admin_token:
            return
        raise HTTPException(401, "Требуется авторизация")

    def serialize(post: dict) -> dict:
        post = dict(post)
        post["media"] = [{**m, "url": f"/media/{m['file']}"} for m in post["media"]]
        post["link"] = publisher.link(post["message_ids"][0]) if post["message_ids"] else None
        return post

    def get_editable(post_id: int) -> dict:
        post = db.get_post(post_id)
        if not post:
            raise HTTPException(404, "Пост не найден")
        if post["status"] not in EDITABLE:
            raise HTTPException(409, "Опубликованный пост нельзя изменить")
        return post

    def next_slot() -> datetime:
        return next_free_slot(utcnow(), settings.slots, settings.tz, db.scheduled_times())

    api = Depends(require_admin)

    @app.get("/api/channel", dependencies=[api])
    async def channel() -> dict:
        avatar = settings.media_dir / "_channel.jpg"
        return {
            "title": publisher.channel_title,
            "username": publisher.channel_username,
            "avatar": "/media/_channel.jpg" if avatar.exists() else None,
            "stats": db.stats(),
            "timezone": settings.timezone,
            "slots": [s.strftime("%H:%M") for s in settings.slots],
            "next_slot": next_slot().isoformat(),
        }

    @app.get("/api/posts", dependencies=[api])
    async def list_posts(
        status: Literal["draft", "scheduled", "publishing", "published", "failed"] | None = None,
        limit: int = Query(30, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ) -> list[dict]:
        return [serialize(p) for p in db.list_posts(status, limit, offset)]

    @app.post("/api/posts", dependencies=[api], status_code=201)
    async def create_post(
        caption: str = Form(""),
        mode: Literal["now", "queue", "at", "draft"] = Form("queue"),
        scheduled_at: str | None = Form(None),
        files: list[UploadFile] = File(default=[]),
    ) -> dict:
        files = [f for f in files if f.filename]
        if not files and not caption.strip():
            raise HTTPException(422, "Добавьте фото/видео или текст")
        if len(files) > MAX_MEDIA:
            raise HTTPException(422, f"Не больше {MAX_MEDIA} файлов в одном посте")
        if len(caption) > caption_limit(files):
            raise HTTPException(422, f"Текст длиннее {caption_limit(files)} символов")
        when = None
        if mode == "at":
            if not scheduled_at:
                raise HTTPException(422, "Укажите время публикации")
            when = parse_when(scheduled_at)
        elif mode == "queue":
            when = next_slot()
        elif mode == "now":
            when = utcnow()

        media = []
        for upload in files:
            ctype = upload.content_type or ""
            if not ctype.startswith(("image/", "video/")):
                raise HTTPException(422, f"Неподдерживаемый файл: {upload.filename}")
            kind = "video" if ctype.startswith("video/") else "photo"
            ext = Path(upload.filename).suffix.lower()[:5] or (".mp4" if kind == "video" else ".jpg")
            name = f"{uuid.uuid4().hex}{ext}"
            (settings.media_dir / name).write_bytes(await upload.read())
            media.append({"file": name, "type": kind})

        post = db.create_post(caption, media, "draft" if mode == "draft" else "scheduled", when)
        if mode == "now":
            post = await scheduler.publish_post(post["id"]) or db.get_post(post["id"])
        return serialize(post)

    @app.patch("/api/posts/{post_id}", dependencies=[api])
    async def update_post(post_id: int, patch: PostPatch) -> dict:
        post = get_editable(post_id)
        fields: dict = {}
        if patch.caption is not None:
            if len(patch.caption) > caption_limit(post["media"]):
                raise HTTPException(422, f"Текст длиннее {caption_limit(post['media'])} символов")
            if not patch.caption.strip() and not post["media"]:
                raise HTTPException(422, "Текстовый пост не может быть пустым")
            fields["caption"] = patch.caption
        if patch.scheduled_at is not None:
            if patch.scheduled_at.tzinfo is None:
                raise HTTPException(422, "Дата должна содержать часовой пояс")
            fields["scheduled_at"] = patch.scheduled_at
            fields["status"] = "scheduled"
        if patch.status == "draft":
            fields.update(status="draft", scheduled_at=None)
        elif patch.status == "scheduled" and "scheduled_at" not in fields:
            # черновик -> в очередь; упавший пост -> повтор в прежнее (уже прошедшее) время, т.е. сразу
            fields.update(status="scheduled", scheduled_at=post["scheduled_at"] or next_slot())
        if fields.get("status") == "scheduled":
            fields["error"] = None
        return serialize(db.update_post(post_id, **fields))

    @app.post("/api/posts/{post_id}/publish", dependencies=[api])
    async def publish_now(post_id: int) -> dict:
        get_editable(post_id)
        post = await scheduler.publish_post(post_id, EDITABLE)
        if post is None:
            raise HTTPException(409, "Пост уже публикуется")
        return serialize(post)

    @app.delete("/api/posts/{post_id}", dependencies=[api], status_code=204)
    async def delete_post(post_id: int, from_channel: bool = False) -> None:
        post = db.get_post(post_id)
        if not post:
            raise HTTPException(404, "Пост не найден")
        if post["status"] == "publishing":
            raise HTTPException(409, "Пост сейчас публикуется")
        if from_channel and post["message_ids"]:
            try:
                await publisher.delete(post["message_ids"])
            except Exception as exc:
                raise HTTPException(502, f"Telegram не дал удалить сообщение: {exc}")
        db.delete_post(post_id)
        for item in post["media"]:
            (settings.media_dir / item["file"]).unlink(missing_ok=True)

    app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


def run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()

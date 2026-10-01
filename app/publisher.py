import asyncio
import logging
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo

from .db import Database, utcnow

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024  # подпись к фото/видео
TEXT_LIMIT = 4096  # текстовый пост
MAX_MEDIA = 10  # альбом в Telegram


def caption_limit(media: list) -> int:
    return CAPTION_LIMIT if media else TEXT_LIMIT


class Publisher:
    """Отправляет посты в канал через Bot API."""

    def __init__(self, bot: Bot, channel_id: str | int, media_dir: Path):
        self.bot = bot
        self.channel_id = channel_id
        self.media_dir = media_dir
        self.channel_username: str | None = None
        self.channel_title: str = str(channel_id)

    async def load_channel_info(self) -> None:
        """Название, @username и аватар канала для шапки ленты (best-effort)."""
        try:
            chat = await self.bot.get_chat(self.channel_id)
            self.channel_title = chat.title or self.channel_title
            self.channel_username = chat.username
            if chat.photo:
                await self.bot.download(chat.photo.big_file_id, destination=self.media_dir / "_channel.jpg")
        except Exception:
            log.exception("Не удалось получить информацию о канале %s", self.channel_id)

    def link(self, message_id: int) -> str | None:
        if self.channel_username:
            return f"https://t.me/{self.channel_username}/{message_id}"
        channel = str(self.channel_id)
        if channel.startswith("-100"):
            return f"https://t.me/c/{channel[4:]}/{message_id}"
        return None

    def _file(self, item: dict) -> FSInputFile:
        return FSInputFile(self.media_dir / item["file"])

    async def publish(self, post: dict) -> list[int]:
        media, caption = post["media"], post["caption"] or None
        if not media:
            msg = await self.bot.send_message(self.channel_id, caption or "")
            return [msg.message_id]
        if len(media) == 1:
            item = media[0]
            if item["type"] == "video":
                msg = await self.bot.send_video(self.channel_id, self._file(item), caption=caption)
            else:
                msg = await self.bot.send_photo(self.channel_id, self._file(item), caption=caption)
            return [msg.message_id]
        group = []
        for i, item in enumerate(media[:MAX_MEDIA]):
            cls = InputMediaVideo if item["type"] == "video" else InputMediaPhoto
            group.append(cls(media=self._file(item), caption=caption if i == 0 else None))
        messages = await self.bot.send_media_group(self.channel_id, group)
        return [m.message_id for m in messages]

    async def delete(self, message_ids: list[int]) -> None:
        if message_ids:
            await self.bot.delete_messages(self.channel_id, message_ids)


class Scheduler:
    """Фоновый цикл: публикует посты, у которых наступило время."""

    def __init__(self, db: Database, publisher: Publisher, interval: float = 15.0):
        self.db = db
        self.publisher = publisher
        self.interval = interval

    async def publish_post(self, post_id: int, from_statuses: tuple[str, ...] = ("scheduled",)) -> dict | None:
        if not self.db.claim_for_publishing(post_id, from_statuses):
            return None  # уже публикуется или опубликован
        post = self.db.get_post(post_id)
        try:
            ids = await self.publisher.publish(post)
        except Exception as exc:
            log.exception("Ошибка публикации поста #%s", post_id)
            return self.db.update_post(post_id, status="failed", error=str(exc))
        log.info("Пост #%s опубликован: %s", post_id, ids)
        return self.db.update_post(post_id, status="published", published_at=utcnow(), message_ids=ids, error=None)

    async def tick(self, now: datetime | None = None) -> int:
        due = self.db.due_posts(now or utcnow())
        for post in due:
            await self.publish_post(post["id"])
        return len(due)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Ошибка в планировщике")
            await asyncio.sleep(self.interval)

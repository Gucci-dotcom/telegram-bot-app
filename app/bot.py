"""Бот: админ пересылает/присылает пост — он встаёт в очередь на ближайший слот."""
import uuid
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

from .config import Settings
from .db import Database, from_iso, utcnow
from .publisher import Scheduler, caption_limit
from .slots import next_free_slot


def fmt_local(dt: datetime, settings: Settings) -> str:
    return dt.astimezone(settings.tz).strftime("%d.%m %H:%M")


def post_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🚀 Сейчас", callback_data=f"now:{post_id}"),
                InlineKeyboardButton(text="🗑 Отменить", callback_data=f"del:{post_id}"),
            ]
        ]
    )


def build_router(settings: Settings, db: Database, scheduler: Scheduler) -> Router:
    router = Router()
    is_admin = F.from_user.id.in_(settings.admin_ids)
    router.message.filter(F.chat.type == "private")

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if message.from_user.id not in settings.admin_ids:
            await message.answer(
                f"Это бот-публикатор канала. Ваш id: <code>{message.from_user.id}</code> — "
                "добавьте его в ADMIN_IDS, чтобы управлять публикациями.",
                parse_mode="HTML",
            )
            return
        markup = None
        if settings.webapp_url.startswith("https://"):
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="📱 Открыть ленту", web_app=WebAppInfo(url=settings.webapp_url))]]
            )
        slots = ", ".join(s.strftime("%H:%M") for s in settings.slots) or "—"
        await message.answer(
            "Пришлите фото, видео или текст — пост встанет в очередь на ближайший свободный слот "
            f"({slots}, {settings.timezone}).\n\n/queue — очередь публикаций",
            reply_markup=markup,
        )

    @router.message(Command("queue"), is_admin)
    async def queue(message: Message) -> None:
        posts = db.list_posts("scheduled", limit=20)
        if not posts:
            await message.answer("Очередь пуста.")
            return
        lines = []
        for p in posts:
            preview = (p["caption"] or "").replace("\n", " ")[:40] or ("📷" * len(p["media"]))
            lines.append(f"#{p['id']} · {fmt_local(from_iso(p['scheduled_at']), settings)} · {preview}")
        await message.answer("Очередь:\n" + "\n".join(lines))

    @router.message(is_admin, F.photo | F.video | F.text)
    async def enqueue(message: Message, bot: Bot) -> None:
        if message.text and message.text.startswith("/"):
            return
        media = []
        if message.photo or message.video:
            obj, kind, ext = (message.photo[-1], "photo", "jpg") if message.photo else (message.video, "video", "mp4")
            name = f"{uuid.uuid4().hex}.{ext}"
            await bot.download(obj.file_id, destination=settings.media_dir / name)
            media.append({"file": name, "type": kind})
        caption = message.caption or message.text or ""
        if len(caption) > caption_limit(media):
            await message.answer(f"Слишком длинный текст: {len(caption)} > {caption_limit(media)} символов.")
            return
        when = next_free_slot(utcnow(), settings.slots, settings.tz, db.scheduled_times())
        post = db.create_post(caption, media, "scheduled", when)
        await message.answer(
            f"✅ Пост #{post['id']} запланирован на {fmt_local(when, settings)}",
            reply_markup=post_keyboard(post["id"]),
        )

    @router.callback_query(is_admin, F.data.startswith("now:"))
    async def publish_now(call: CallbackQuery) -> None:
        post = await scheduler.publish_post(int(call.data.split(":")[1]), ("scheduled", "draft", "failed"))
        if post is None:
            await call.answer("Пост уже опубликован или удалён")
        elif post["status"] == "published":
            await call.message.edit_text(f"🚀 Пост #{post['id']} опубликован")
        else:
            await call.message.edit_text(f"⚠️ Ошибка публикации #{post['id']}: {post['error']}")

    @router.callback_query(is_admin, F.data.startswith("del:"))
    async def cancel(call: CallbackQuery) -> None:
        post = db.get_post(int(call.data.split(":")[1]))
        if not post or post["status"] not in ("scheduled", "draft", "failed"):
            await call.answer("Нельзя отменить")
            return
        db.delete_post(post["id"])
        await call.message.edit_text(f"🗑 Пост #{post['id']} удалён из очереди")

    return router


async def setup_menu_button(bot: Bot, settings: Settings) -> None:
    """Кнопка «Лента» слева от поля ввода — открывает Mini App."""
    if settings.webapp_url.startswith("https://"):
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Лента", web_app=WebAppInfo(url=settings.webapp_url))
        )

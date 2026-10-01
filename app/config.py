import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


def parse_slots(raw: str) -> list[time]:
    """'09:00, 13:30' -> [time(9, 0), time(13, 30)] (отсортировано, без дублей)."""
    slots = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        hours, minutes = part.split(":")
        slots.add(time(int(hours), int(minutes)))
    return sorted(slots)


def parse_ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


@dataclass
class Settings:
    bot_token: str
    channel_id: str
    admin_ids: set[int] = field(default_factory=set)
    admin_token: str = ""
    webapp_url: str = ""
    timezone: str = "Europe/Moscow"
    slots: list[time] = field(default_factory=lambda: parse_slots("09:00,13:00,19:00"))
    db_path: Path = Path("data/posts.db")
    media_dir: Path = Path("data/media")
    host: str = "0.0.0.0"
    port: int = 8000
    poll_interval: float = 15.0

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        env = os.environ
        missing = [k for k in ("BOT_TOKEN", "CHANNEL_ID") if not env.get(k)]
        if missing:
            raise RuntimeError(f"Не заданы переменные окружения: {', '.join(missing)} (см. .env.example)")
        return cls(
            bot_token=env["BOT_TOKEN"],
            channel_id=env["CHANNEL_ID"],
            admin_ids=parse_ids(env.get("ADMIN_IDS", "")),
            admin_token=env.get("ADMIN_TOKEN", ""),
            webapp_url=env.get("WEBAPP_URL", "").rstrip("/"),
            timezone=env.get("TIMEZONE", "Europe/Moscow"),
            slots=parse_slots(env.get("SCHEDULE_SLOTS", "09:00,13:00,19:00")),
            db_path=Path(env.get("DB_PATH", "data/posts.db")),
            media_dir=Path(env.get("MEDIA_DIR", "data/media")),
            host=env.get("HOST", "0.0.0.0"),
            port=int(env.get("PORT", "8000")),
            poll_interval=float(env.get("POLL_INTERVAL", "15")),
        )

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def next_free_slot(
    now: datetime,
    slots: list[time],
    tz: ZoneInfo,
    taken: list[datetime],
    max_days: int = 365,
) -> datetime:
    """Ближайший слот расписания (в часовом поясе канала) позже `now`, ещё не занятый постом.

    Возвращает datetime в UTC. Если слоты не заданы — публикация через минуту.
    """
    if not slots:
        return now.astimezone(timezone.utc) + timedelta(minutes=1)
    busy = {t.astimezone(timezone.utc).replace(second=0, microsecond=0) for t in taken}
    local_now = now.astimezone(tz)
    for day in range(max_days):
        date = (local_now + timedelta(days=day)).date()
        for slot in slots:
            candidate = datetime.combine(date, slot, tzinfo=tz).astimezone(timezone.utc)
            if candidate > now and candidate not in busy:
                return candidate
    raise RuntimeError("Нет свободных слотов в расписании")

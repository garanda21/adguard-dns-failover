"""Human readable (Spanish) description of the cron schedules."""

from __future__ import annotations

import re
from datetime import datetime

from croniter import croniter

DAY_NAMES = {
    "Monday": "lunes", "Tuesday": "martes", "Wednesday": "miércoles", "Thursday": "jueves",
    "Friday": "viernes", "Saturday": "sábado", "Sunday": "domingo",
}


def describe_cron(expr: str) -> str:
    """Describe one cron expression in Spanish; fall back to the raw expression."""
    try:
        from cron_descriptor import Options, get_description

        options = Options()
        options.locale_code = "es_ES"
        options.use_24hour_time_format = True
        text = get_description(expr, options)
    except Exception:  # noqa: BLE001 - never break notifications over a description
        return expr
    # Day names come from the system locale (English in the container).
    text = re.sub("|".join(DAY_NAMES), lambda m: DAY_NAMES[m.group(0)], text)
    return text[0].lower() + text[1:]


def next_check(crons: list[str], now: datetime) -> datetime:
    return min(croniter(expr, now).get_next(datetime) for expr in crons)

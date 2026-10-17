from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def money(value):
    try:
        return f"${Decimal(value):,.2f}"
    except Exception:
        return value


@register.filter
def duration(seconds):
    """7h 30m style."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours}h {minutes:02d}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


@register.filter
def hours(seconds):
    try:
        return f"{int(seconds) / 3600:.2f}"
    except (TypeError, ValueError):
        return ""


@register.filter
def minutes_label(value):
    """Turn late_minutes into words."""
    if value is None:
        return ""
    value = int(value)
    if value == 0:
        return "right on time"
    word = "late" if value > 0 else "early"
    value = abs(value)
    if value >= 60:
        h, m = divmod(value, 60)
        text = f"{h}h {m}m" if m else f"{h}h"
    else:
        text = f"{value} min"
    return f"{text} {word}"

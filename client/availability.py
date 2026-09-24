"""Validate range-level inventory before fetching exact slots for available dates."""

from datetime import date


def calendar_available_dates(data, requested_days):
    try:
        if not isinstance(data, dict) or not isinstance(data['scheduled'], list):
            raise ValueError
        horizon = data['last_calendar_day']
        if not isinstance(horizon, str) or date.fromisoformat(horizon).isoformat() != horizon:
            raise ValueError
        expected = {day for day in requested_days if day <= horizon}
        seen = set()
        available = set()
        for entry in data['scheduled']:
            day = entry['date']
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                raise ValueError
            if day not in expected or day in seen:
                raise ValueError
            inventory = entry['inventory']['reservation']
            if inventory not in ('available', 'sold-out', 'closed'):
                raise ValueError
            seen.add(day)
            if inventory == 'available':
                available.add(day)
        if seen != expected:
            raise ValueError
        return [day for day in requested_days if day in available]
    except (KeyError, TypeError, ValueError):
        raise ValueError(
            'Calendar response is incomplete or unsupported; availability is unresolved.'
        ) from None

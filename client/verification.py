"""Read-only confirmation checks; never treat a booking HTTP response as proof."""

from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from client.booking_state import reservation_reference


class VerificationError(Exception):
    pass


def upcoming(headers, proxies=None, timeout=(3, 5)):
    try:
        response = requests.get(
            'https://api.resy.com/3/user/reservations',
            params={'type': 'upcoming'},
            headers=headers,
            proxies=proxies or {},
            timeout=timeout,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise VerificationError(f'Account verification failed (HTTP {response.status_code}).')
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get('reservations'), list):
            raise VerificationError('Unexpected account response; verification is unresolved.')
        return data
    except (requests.RequestException, ValueError):
        raise VerificationError('Account verification could not complete.') from None


def row_details(row):
    venue = row.get('venue', {})
    venue_id = venue.get('id') if isinstance(venue, dict) else venue
    if isinstance(venue_id, dict):
        venue_id = venue_id.get('resy')
    clock = str(row.get('time_slot', ''))[:5]
    return str(venue_id), row.get('day'), clock, row.get('num_seats')


def matching_reservation(data, venue_id, day, clock, party):
    matches = [row for row in data['reservations'] if row_details(row) == (str(venue_id), day, clock, party)]
    if len(matches) > 1:
        raise VerificationError('Multiple matching reservations; inspect your account.')
    if not matches:
        return None
    return reservation_reference(matches[0])


def slot_datetime(slot, day, time_zone='America/New_York'):
    raw = slot.get('date', {}).get('start')
    if not isinstance(raw, str):
        raise ValueError('Slot did not expose an explicit date and time.')
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo(time_zone))
    if parsed.date().isoformat() != day:
        raise ValueError('Slot date does not match the requested date.')
    return parsed.strftime('%H:%M')

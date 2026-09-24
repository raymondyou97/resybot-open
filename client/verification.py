"""Read-only confirmation checks; never treat a booking HTTP response as proof."""

from datetime import date, datetime
import re
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
        validated_reservations(data)
        return data
    except (requests.RequestException, ValueError):
        raise VerificationError('Account verification could not complete.') from None


def row_details(row):
    try:
        if not isinstance(row, dict):
            raise ValueError
        venue = row.get('venue')
        venue_id = venue.get('id') if isinstance(venue, dict) else venue
        if isinstance(venue_id, dict):
            venue_id = venue_id.get('resy')
        if not re.fullmatch(r'[0-9]+', str(venue_id)) or int(venue_id) <= 0:
            raise ValueError
        day, clock, party = row.get('day'), row.get('time_slot'), row.get('num_seats')
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError
        if not isinstance(clock, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9](?::00)?', clock):
            raise ValueError
        if type(party) is not int or party <= 0:
            raise ValueError
        return str(venue_id), day, clock[:5], party
    except (TypeError, ValueError):
        raise VerificationError(
            'Incomplete or unsupported reservation details; verification is unresolved.'
        ) from None


def validated_reservations(data):
    if not isinstance(data, dict) or not isinstance(data.get('reservations'), list):
        raise VerificationError('Unexpected account response; verification is unresolved.')
    rows = data['reservations']
    for row in rows:
        row_details(row)
        try:
            reservation_reference(row)
        except ValueError:
            raise VerificationError(
                'Reservation reference is missing or invalid; verification is unresolved.'
            ) from None
    return rows


def matching_reservation(data, venue_id, day, clock, party):
    matches = [
        row for row in validated_reservations(data) if row_details(row) == (str(venue_id), day, clock, party)
    ]
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
    if parsed.second or parsed.microsecond:
        raise ValueError('Slot time must have exact minute precision.')
    return parsed.strftime('%H:%M')

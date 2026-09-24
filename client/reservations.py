"""Reservation listing and cancellation that requires account verification."""

from datetime import datetime
import time
from zoneinfo import ZoneInfo

import requests

from client.booking_state import BookingState, account_key, reservation_reference
from client.config_store import load_data, save_data
from client.fees import FeePolicyError, amount
from client.task_executor import resy_headers, safe_label
from client.verification import VerificationError, matching_reservation, row_details, upcoming


CACHE = 'resrevations.json'


def list_account(account):
    data = upcoming(resy_headers(account['auth_token']))
    records = []
    for row in data['reservations']:
        venue, day, clock, party = row_details(row)
        name = data.get('venues', {}).get(venue, {}).get('name', f'Restaurant {venue}')
        records.append(
            {
                'account_name': account['account_name'],
                'venue_id': venue,
                'venue': safe_label(name),
                'day': day,
                'time_slot': clock,
                'num_seats': party,
                'reference': reservation_reference(row),
            }
        )
    return records


def cancellation_cost(row, now):
    try:
        fee = row['cancellation']['fee']
        cost = amount(fee['amount'])
        if cost == 0:
            return cost
        cutoff = datetime.fromisoformat(fee['date_cut_off'].replace('Z', '+00:00'))
        if cutoff.tzinfo is None:
            raise ValueError('Missing timezone')
        party = row['num_seats']
        if isinstance(party, bool) or not isinstance(party, int) or party < 1:
            raise ValueError('Missing party size')
        return amount(0) if now < cutoff else cost * party
    except (KeyError, TypeError, ValueError):
        raise FeePolicyError('Cancellation charge could not be verified; use the normal Resy UI.') from None


def cancel_verified(account, record, max_fee=0, state=None):
    headers = resy_headers(account['auth_token'])
    state = state or BookingState()
    now = datetime.now(ZoneInfo('America/New_York'))
    target = datetime.fromisoformat(f'{record["day"]}T{record["time_slot"]}').replace(tzinfo=now.tzinfo)
    if target <= now:
        raise VerificationError('Cannot verify cancellation of a past reservation from Upcoming.')
    if record.get('cancellation_pending'):
        raise VerificationError(
            'Prior cancellation is unresolved; verify the account before another request.'
        )
    rows = upcoming(headers)['reservations']
    current = next((row for row in rows if reservation_reference(row) == record['reference']), None)
    if current is None:
        raise VerificationError('Reservation not found before cancellation; local record retained.')
    if row_details(current) != (
        str(record['venue_id']),
        record['day'],
        record['time_slot'],
        record['num_seats'],
    ):
        raise VerificationError('Reservation details changed; refresh and review before cancelling.')
    if cancellation_cost(current, now) > amount(max_fee):
        raise FeePolicyError('Cancellation charge exceeds the approved ceiling.')
    cached = load_data(CACHE, [])
    for item in cached:
        if item.get('reference') == record['reference']:
            item['cancellation_pending'] = True
    save_data(CACHE, cached)
    for claim in state.rows():
        if claim['reservation_ref'] == record['reference'] and claim['status'] == 'confirmed':
            state.transition(claim['id'], 'confirmed', 'cancellation-pending')
    try:
        response = requests.post(
            'https://api.resy.com/3/cancel',
            headers=headers,
            data={'resy_token': current['resy_token']},
            timeout=(3, 5),
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise VerificationError('Cancellation was not confirmed; local record retained.')
        after = upcoming(headers)
        if any(reservation_reference(row) == record['reference'] for row in after['reservations']):
            raise VerificationError('Reservation still appears in account; local record retained.')
        if datetime.now(now.tzinfo) >= target:
            raise VerificationError('Reservation time passed during verification; local record retained.')
    except (requests.RequestException, ValueError, KeyError):
        raise VerificationError('Cancellation outcome is uncertain; local record retained.') from None
    save_data(CACHE, [item for item in cached if item.get('reference') != record['reference']])
    for claim in state.rows():
        if claim['reservation_ref'] == record['reference'] and claim['status'] == 'cancellation-pending':
            state.transition(claim['id'], 'cancellation-pending', 'cancelled')
    return True


def resolve_claim(state, claim_id, account, *, checked_no_booking=False):
    claim = state.get(claim_id)
    if not claim or claim['account'] != account_key(account):
        raise VerificationError('Claim/account mismatch.')
    data = upcoming(resy_headers(account['auth_token']))
    reference = matching_reservation(data, claim['venue'], claim['day'], claim['clock'], claim['party'])
    if reference and claim['status'] in {'submitted', 'dispatching'}:
        state.confirmed(claim_id, reference)
        return 'confirmed'
    if reference:
        raise VerificationError('A matching reservation exists; hold retained.')
    now = datetime.now(ZoneInfo('America/New_York'))
    target = datetime.fromisoformat(f'{claim["day"]}T{claim["clock"]}').replace(tzinfo=now.tzinfo)
    if target <= now:
        raise VerificationError('Past reservations require independent review; hold retained.')
    if claim['status'] == 'cancellation-pending':
        if any(reservation_reference(row) == claim['reservation_ref'] for row in data['reservations']):
            raise VerificationError(
                'The reservation still exists, possibly with changed details; hold retained.'
            )
        state.transition(claim_id, 'cancellation-pending', 'cancelled')
        cached = load_data(CACHE, [])
        save_data(CACHE, [row for row in cached if row.get('reference') != claim['reservation_ref']])
        return 'cancelled'
    if checked_no_booking and claim['status'] in {'claimed', 'submitted', 'dispatching'}:
        if any(row_details(row)[0] == claim['venue'] for row in data['reservations']):
            raise VerificationError('Another reservation at this venue needs review; hold retained.')
        if time.time() - claim['created'] < 30:
            raise VerificationError('Allow the in-flight attempt to settle before releasing a hold.')
        state.transition(claim_id, claim['status'], 'released')
        return 'released'
    raise VerificationError('No verified success; hold retained. Check account and email before releasing.')

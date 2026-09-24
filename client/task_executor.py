"""Bounded automatic reservation workers with durable submission holds."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from client.booking_state import BookingState, CampaignBlocked
from client.control import RunControl
from client.fees import FeePolicyError, validate_policy, validate_quote
from client.http_headers import CLIENT_USER_AGENT
from client.local_auth import local_headers
from client.time_window import minute_value, time_window
from client.verification import matching_reservation, row_details, slot_datetime, upcoming


PUBLIC_CLIENT_KEY = 'VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5'


def resy_headers(auth_token):
    return {
        'X-Resy-Auth-Token': auth_token,
        'Authorization': f'ResyAPI api_key="{PUBLIC_CLIENT_KEY}"',
        'X-Resy-Universal-Auth': auth_token,
        'User-Agent': CLIENT_USER_AGENT,
        'Accept': 'application/json',
        'Referer': 'https://resy.com/',
    }


def format_proxy(proxy_str):
    ip, port, user, password = proxy_str.split(':')
    return {scheme: f'http://{user}:{password}@{ip}:{port}' for scheme in ('http', 'https')}


def reservation_dates(start_date, end_date):
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start.isoformat() != start_date or end.isoformat() != end_date:
        raise ValueError('Reservation dates must use YYYY-MM-DD.')
    count = (end - start).days + 1
    if not 1 <= count <= 31:
        raise ValueError('Reservation date range must contain 1–31 days.')
    return [(start + timedelta(days=offset)).isoformat() for offset in range(count)]


def safe_label(name):
    return ''.join(char for char in str(name) if char.isprintable())[:160]


def execute_task(
    task,
    capsolver_key='',
    capmonster_key='',
    proxies=None,
    webhook_url='',
    *,
    control=None,
    dry_run=True,
    state=None,
):
    control = control or RunControl()
    label = 'Restaurant unknown'
    claim = None
    submitted = False
    state = state if state is not None else (None if dry_run else BookingState())

    def report(text):
        send_discord_notification(webhook_url, summary=f'[{label}] {text}')

    try:
        venue_id = str(task['restaurant_id'])
        party = task['party_sz']
        if (
            not venue_id.isdigit()
            or isinstance(party, bool)
            or not isinstance(party, int)
            or not 1 <= party <= 20
        ):
            raise ValueError('Invalid restaurant ID or party size.')
        label = f'Restaurant {venue_id}'
        start_minute, end_minute = time_window(task)
        pause_seconds = max(float(task['delay']) / 1000, 1.0)
        if not 0 < pause_seconds <= 86400:
            raise ValueError('Invalid polling delay.')
        days = reservation_dates(task['start_date'], task['end_date'])
        if not dry_run:
            validate_policy(task)
        headers = resy_headers(task['auth_token'])
        proxy = format_proxy(proxies[0]) if proxies else {}
        while not control.stopped():
            today = datetime.now(ZoneInfo(task.get('timezone', 'America/New_York'))).date()
            future_days = [day for day in days if date.fromisoformat(day) >= today]
            if not future_days:
                report('All requested dates are in the past; stopped.')
                return 'stopped'
            for day in future_days:
                if control.stopped():
                    return 'stopped'
                if state and state.blocked(task):
                    report('Campaign already succeeded or has an unresolved attempt; stopped.')
                    return 'blocked'
                response = requests.get(
                    'https://api.resy.com/4/find',
                    params={'lat': 0, 'long': 0, 'day': day, 'party_size': party, 'venue_id': venue_id},
                    headers=headers,
                    proxies=proxy,
                    timeout=control.timeout(),
                    allow_redirects=False,
                )
                if control.stopped():
                    return 'stopped'
                if response.status_code != 200:
                    report(f'Availability failed (HTTP {response.status_code}); stopped without retry.')
                    return 'failed'
                data = response.json()
                venues = data.get('results', {}).get('venues') if isinstance(data, dict) else None
                if not isinstance(venues, list):
                    raise ValueError('Unexpected slot response.')
                for venue in venues:
                    metadata = venue.get('venue', {})
                    actual_id = metadata.get('id')
                    if isinstance(actual_id, dict):
                        actual_id = actual_id.get('resy')
                    if str(actual_id) != venue_id:
                        continue
                    if metadata.get('name'):
                        label = f'{safe_label(metadata["name"])} (ID {venue_id})'
                    for slot in venue.get('slots', []):
                        timezone = ZoneInfo(task.get('timezone', 'America/New_York'))
                        clock = slot_datetime(slot, day, str(timezone))
                        slot_at = datetime.fromisoformat(f'{day}T{clock}').replace(tzinfo=timezone)
                        if slot_at <= datetime.now(timezone):
                            continue
                        if not start_minute <= minute_value(clock) <= end_minute:
                            continue
                        if dry_run:
                            print(f'[{label}] DRY RUN: matching slot {day} {clock}; checkout not entered.')
                            continue
                        if control.stopped():
                            return 'stopped'
                        before = upcoming(headers, proxy, timeout=control.timeout())
                        if any(row_details(row)[0] == venue_id for row in before['reservations']):
                            report('This account already has an upcoming reservation at this venue; stopped.')
                            return 'blocked'
                        if control.stopped():
                            return 'stopped'
                        claim = state.claim(task, day, clock)
                        config_token = slot['config']['token']
                        quote = get_details(
                            day,
                            party,
                            config_token,
                            venue_id,
                            headers,
                            proxy,
                            control=control,
                            claim_id=claim,
                        )
                        validate_quote(task, quote['details'])
                        if control.stopped():
                            return 'stopped'
                        state.submitted(claim)
                        submitted = True
                        book_reservation(
                            quote['response_value'],
                            task['auth_token'],
                            task['payment_id'],
                            day,
                            party,
                            venue_id,
                            config_token,
                            headers,
                            proxy,
                            control=control,
                            claim_id=claim,
                        )
                        # Verification must still run after a submitted request reaches its deadline.
                        account = upcoming(headers, proxy)
                        reference = matching_reservation(account, venue_id, day, clock, party)
                        if reference:
                            state.confirmed(claim, reference)
                            report(f'Confirmed in account: {day} {clock}, party {party}. Campaign stopped.')
                            return 'confirmed'
                        report('Submission was not verified in the account. Hold retained; do not retry.')
                        return 'awaiting-verification'
                print(f'[{label}] No further matching slots for {day}; waiting {pause_seconds:g} seconds.')
                if control.wait(pause_seconds):
                    return 'stopped'
            if dry_run:
                return 'dry-run-complete'
    except CampaignBlocked:
        report('Campaign already claimed or completed; stopped.')
        return 'blocked'
    except Exception as error:
        if submitted:
            report('Submission outcome is uncertain. Hold retained; inspect account before retrying.')
            return 'awaiting-verification'
        # Error types are safe; network exception strings may contain credentials or response data.
        if isinstance(error, FeePolicyError):
            report(str(error))
        else:
            report(f'Task stopped before submission ({type(error).__name__}); inspect configuration locally.')
        return 'failed'
    finally:
        if claim and not submitted:
            state.release_unsubmitted(claim)
    return 'stopped'


def get_details(
    day, party_size, config_token, restaurant_id, headers, select_proxy, *, control=None, claim_id=None
):
    response = requests.post(
        'http://127.0.0.1:8000/api/get-details',
        headers=local_headers(),
        json={
            'day': day,
            'party_size': party_size,
            'config_token': config_token,
            'claim_id': claim_id,
            'restaurant_id': restaurant_id,
            'headers': headers,
            'select_proxy': select_proxy,
        },
        timeout=control.timeout() if control else (3, 5),
        allow_redirects=False,
    )
    if response.status_code != 200:
        raise ValueError('Reservation details were unavailable.')
    data = response.json()
    if (
        not isinstance(data, dict)
        or not data.get('response_value')
        or not isinstance(data.get('details'), dict)
    ):
        raise ValueError('Incomplete reservation details.')
    return data


def book_reservation(
    book_token,
    auth_token,
    payment_id,
    day,
    party_size,
    restaurant_id,
    config_token,
    headers,
    select_proxy,
    *,
    control=None,
    claim_id=None,
):
    if control and control.stopped():
        raise ValueError('Task stopped before booking dispatch; verification hold retained.')
    response = requests.post(
        'http://127.0.0.1:8000/api/book-reservation',
        headers=local_headers(),
        json={
            'book_token': book_token,
            'payment_id': payment_id,
            'day': day,
            'party_size': party_size,
            'restaurant_id': restaurant_id,
            'headers': headers,
            'select_proxy': select_proxy,
            'claim_id': claim_id,
        },
        timeout=control.timeout() if control else (3, 5),
        allow_redirects=False,
    )
    if response.status_code not in (200, 201):
        raise ValueError('Booking response is uncertain; verify account before retrying.')
    return response.json()


def send_discord_notification(webhook_url, message=None, *, summary='Task ended; verify status in Resy.'):
    print(summary)
    if not webhook_url:
        return
    parsed = urlparse(webhook_url)
    if (
        parsed.scheme != 'https'
        or parsed.hostname not in {'discord.com', 'discordapp.com'}
        or not parsed.path.startswith('/api/webhooks/')
    ):
        print('Discord notification skipped: unsupported destination.')
        return
    try:
        response = requests.post(
            webhook_url, json={'content': summary}, timeout=(3, 5), allow_redirects=False
        )
        if response.status_code not in (200, 204):
            print('Discord notification failed; task outcome is unchanged.')
    except requests.RequestException:
        print('Discord notification failed; task outcome is unchanged.')


def run_tasks_concurrently(
    tasks,
    capsolver_key='',
    capmonster_key='',
    proxies=None,
    webhook_url='',
    *,
    control=None,
    dry_run=True,
    state=None,
):
    if not tasks:
        return []
    control = control or RunControl()
    results = []
    with ThreadPoolExecutor(max_workers=min(len(tasks), 4)) as executor:
        futures = [
            executor.submit(
                execute_task,
                task,
                capsolver_key,
                capmonster_key,
                proxies,
                webhook_url,
                control=control,
                dry_run=dry_run,
                state=state,
            )
            for task in tasks
        ]
        try:
            for future in as_completed(futures):
                results.append(future.result())
        except KeyboardInterrupt:
            control.stop()
            for future in futures:
                future.cancel()
            raise
    return results

"""Interactive client. Importing it has no scheduler or network side effects."""

import argparse
from contextlib import contextmanager
import fcntl
from getpass import getpass
import os
import re
import uuid

import inquirer

from client.booking_state import BookingState
from client.config_store import data_path, load_data, save_data
from client.fees import FeePolicyError, amount, validate_policy
from client.reservations import cancel_verified, list_account, resolve_claim
from client.scheduling import TaskManager, next_once, task_id
from client.task_executor import format_proxy, reservation_dates
from client.verification import VerificationError


MANAGER = TaskManager()


def choose(message, choices):
    answer = inquirer.prompt([inquirer.List('choice', message=message, choices=choices)])
    return answer['choice'] if answer else None


def confirm(message):
    return input(f'{message} [y/N] ').strip().lower() == 'y'


def task_summary(task):
    venue = str(task.get('restaurant_id', ''))
    venue = venue if venue.isdigit() else 'invalid'
    dates = [str(task.get(key, '')) for key in ('start_date', 'end_date')]
    dates = [value if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) else 'invalid' for value in dates]
    counts = [task.get(key) for key in ('party_sz', 'start_time', 'end_time')]
    counts = [value if type(value) is int else 'invalid' for value in counts]
    return f'Restaurant {venue}: {dates[0]}–{dates[1]}, party {counts[0]}, hours {counts[1]}–{counts[2]}'


def policy(task):
    task['currency'] = input('Venue currency (e.g. USD): ').strip().upper()
    task['max_total_charge'] = str(amount(input('Maximum TOTAL charge due for this reservation: ')))
    task['max_cancellation_fee'] = str(amount(input('Maximum cancellation/no-show fee amount: ')))
    task['accept_terms'] = confirm('Have you reviewed and accepted the venue booking/cancellation terms?')
    validate_policy(task)


def show_tasks():
    tasks = load_data('tasks.json', [])
    for index, task in enumerate(tasks, 1):
        print(f'{index}) {task_summary(task)}')
    action = choose('Tasks', ['Add task', 'Delete task', 'Configure fee limits/terms', 'Back'])
    if action == 'Add task':
        add_task()
    elif action in {'Delete task', 'Configure fee limits/terms'} and tasks:
        selected = choose('Select task', [(task_summary(task), i) for i, task in enumerate(tasks)])
        if selected is None:
            return
        if action == 'Delete task':
            if not confirm('Delete this task? Existing campaign holds will remain.'):
                return
            tasks.pop(selected)
        else:
            policy(tasks[selected])
        save_data('tasks.json', tasks)


def add_task():
    accounts = load_data('accounts.json', [])
    if not accounts:
        print('Add an account first.')
        return
    index = choose('Account', [(f'Account {i + 1}', i) for i in range(len(accounts))])
    if index is None:
        return
    account = accounts[index]
    task = {key: account[key] for key in ('account_name', 'auth_token', 'payment_id')}
    task['restaurant_id'] = input('Restaurant ID: ').strip()
    if not task['restaurant_id'].isdigit():
        raise ValueError('Invalid restaurant ID')
    task['party_sz'] = int(input('Party size: '))
    task['start_date'] = input('Start date (YYYY-MM-DD): ').strip()
    task['end_date'] = input('End date (YYYY-MM-DD): ').strip()
    reservation_dates(task['start_date'], task['end_date'])
    task['start_time'] = int(input('Earliest hour (0–23): '))
    task['end_time'] = int(input('Latest hour (inclusive, 0–23): '))
    task['delay'] = int(input('Delay between date checks in milliseconds (minimum 1000): '))
    task['campaign_id'] = (
        input('Alternative campaign group (blank = this restaurant): ').strip() or task['restaurant_id']
    )
    policy(task)
    tasks = load_data('tasks.json', [])
    tasks.append(task)
    save_data('tasks.json', tasks)


def manage_accounts():
    accounts = load_data('accounts.json', [])
    print(f'{len(accounts)} saved account(s). Credentials are never displayed.')
    action = choose('Accounts', ['Add account', 'Delete account', 'Back'])
    if action == 'Add account':
        name = input('Stable account alias (must be unique; not a secret): ').strip()
        if not name or any(account['account_name'] == name for account in accounts):
            raise ValueError('Account alias must be unique')
        token = getpass('Resy account token (hidden): ').strip()
        payment = getpass('Saved payment ID (hidden): ').strip()
        if not token or not payment.isdigit():
            raise ValueError('Missing credential')
        if any(account.get('auth_token') == token for account in accounts):
            raise ValueError('This account is already configured')
        accounts.append({'account_name': name, 'auth_token': token, 'payment_id': payment})
        save_data('accounts.json', accounts)
    elif action == 'Delete account' and accounts:
        index = choose('Delete account', [(f'Account {i + 1}', i) for i in range(len(accounts))])
        if index is not None and confirm('Delete this account and its saved tasks? Campaign holds remain.'):
            account = accounts.pop(index)
            save_data('accounts.json', accounts)
            tasks = load_data('tasks.json', [])
            save_data(
                'tasks.json', [task for task in tasks if task.get('account_name') != account['account_name']]
            )


def manage_proxies():
    proxies = load_data('proxies.json', [])
    print(f'{len(proxies)} configured proxy/proxies. Only the first is used; no rotation.')
    action = choose('Proxies', ['Replace proxy', 'Use default connection', 'Back'])
    if action == 'Replace proxy':
        value = getpass('Proxy host:port:username:password (hidden): ').strip()
        format_proxy(value)
        save_data('proxies.json', [value])
    elif action == 'Use default connection':
        save_data('proxies.json', [])


def manage_info():
    info = load_data('info.json', {})
    print('Discord configured:', bool(info.get('discord_webhook')))
    print('CAPTCHA solving is not implemented in the booking worker.')
    action = choose('Notifications', ['Set Discord webhook', 'Disable Discord', 'Back'])
    if action == 'Set Discord webhook':
        info['discord_webhook'] = getpass('Discord webhook (hidden): ').strip()
        save_data('info.json', info)
    elif action == 'Disable Discord' and confirm('Disable Discord notifications?'):
        info.pop('discord_webhook', None)
        save_data('info.json', info)


def start_tasks(dry_run=True, duration=120):
    tasks = load_data('tasks.json', [])
    if not tasks:
        print('No tasks saved.')
        return
    if not dry_run:
        for task in tasks:
            validate_policy(task)
        if not confirm(f'Run {len(tasks)} task(s) with REAL automatic booking enabled?'):
            return
    info, proxies = load_data('info.json', {}), load_data('proxies.json', [])
    for task in tasks:
        started = MANAGER.start(task_id(task), [task], proxies, info, duration=duration, dry_run=dry_run)
        print(f'{task_summary(task)}: {"started" if started else "already running"}.')


def schedule_tasks():
    tasks = load_data('tasks.json', [])
    if not tasks:
        print('No tasks saved.')
        return
    index = choose('Task', [(task_summary(task), i) for i, task in enumerate(tasks)])
    if index is None:
        return
    task = tasks[index]
    repeat = choose('Repeat', ['Once', 'Daily', 'Weekly'])
    if not repeat:
        return
    spec = {
        'id': uuid.uuid4().hex,
        'task_id': task_id(task),
        'repeat': repeat,
        'time': input('Start time HH:MM, America/New_York: ').strip(),
        'duration': float(input('Maximum run duration in seconds: ')),
        'dry_run': not confirm('Enable REAL automatic booking for this schedule?'),
    }
    if not spec['dry_run']:
        validate_policy(task)
    if repeat == 'Weekly':
        spec['weekday'] = choose('Weekday', ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'])
    elif repeat == 'Once':
        spec['at'] = next_once(spec['time'])
    MANAGER.add(spec)
    print('Schedule saved. Client must remain running; missed runs are not caught up.')


def manage_scheduled():
    for key, status in MANAGER.statuses().items():
        print(f'Worker {key[:12]}: {status}')
    specs = load_data('schedules.json', [])
    for spec in specs:
        print(f'Schedule {spec["id"][:12]}: {spec["repeat"]} {spec["time"]} America/New_York')
    action = choose('Manage', ['Stop worker', 'Remove schedule', 'Back'])
    if action == 'Stop worker':
        key = choose('Worker', list(MANAGER.statuses())) if MANAGER.statuses() else None
        if key:
            print(
                'Stopped.' if MANAGER.stop(key) else 'Stopping; waiting for the in-flight request to finish.'
            )
    elif action == 'Remove schedule' and specs:
        key = choose('Schedule', [(spec['id'][:12], spec['id']) for spec in specs])
        if key:
            MANAGER.remove(key)


def list_reservations():
    accounts = load_data('accounts.json', [])
    records = []
    for account in accounts:
        records.extend(list_account(account))
    # Preserve unresolved cancellations even when a refresh no longer lists them.
    pending = [row for row in load_data('resrevations.json', []) if row.get('cancellation_pending')]
    by_reference = {row['reference']: row for row in records}
    by_reference.update({row['reference']: row for row in pending})
    records = list(by_reference.values())
    save_data('resrevations.json', records)
    if not records:
        print('No upcoming reservations found. Unresolved submission holds are NOT released.')
        return
    selected = choose(
        'Reservations',
        [
            (f'{row["venue"]} {row["day"]} {row["time_slot"]}, party {row["num_seats"]}', i)
            for i, row in enumerate(records)
        ]
        + [('Back', -1)],
    )
    if selected is None or selected == -1:
        return
    row = records[selected]
    if confirm('Cancel this exact reservation?'):
        ceiling = amount(input('Maximum cancellation fee you authorize (0 for free cancellation only): '))
        account = next(item for item in accounts if item['account_name'] == row['account_name'])
        if cancel_verified(account, row, max_fee=ceiling):
            print('Cancellation verified in account.')


def manage_holds():
    if any(status != 'finished' for status in MANAGER.statuses().values()):
        print('Stop all active workers and wait for them to finish before resolving holds.')
        return
    state = BookingState()
    rows = state.rows()
    if not rows:
        print('No campaign holds.')
        return
    selected = choose(
        'Campaign holds',
        [(f'{row["venue"]} {row["day"]} {row["clock"]}: {row["status"]}', i) for i, row in enumerate(rows)]
        + [('Back', -1)],
    )
    if selected is None or selected == -1:
        return
    claim = rows[selected]
    accounts = load_data('accounts.json', [])
    from client.booking_state import account_key

    account = next((item for item in accounts if account_key(item) == claim['account']), None)
    if account is None:
        print('Matching account unavailable; hold retained.')
        return
    checked = confirm(
        'Have you checked BOTH the normal Resy account and email and confirmed no booking exists?'
    )
    print('Result:', resolve_claim(state, claim['id'], account, checked_no_booking=checked))


@contextmanager
def client_lock():
    path = data_path('.state/client.lock')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def menu():
    actions = [
        ('1) Show tasks', show_tasks),
        ('2) Proxies', manage_proxies),
        ('3) Info', manage_info),
        ('4) Manage Accounts', manage_accounts),
        ('5) Dry run (no checkout)', lambda: start_tasks(True)),
        ('6) View Reservations', list_reservations),
        ('7) Start Tasks (live)', lambda: start_tasks(False)),
        ('8) Schedule Tasks', schedule_tasks),
        ('9) Manage Scheduled tasks', manage_scheduled),
        ('10) Verify campaign holds', manage_holds),
        ('Exit', None),
    ]
    MANAGER.ensure_scheduler()
    while True:
        action = choose('ResyGrabber', actions)
        if action is None:
            return
        try:
            action()
        except (FeePolicyError, VerificationError) as error:
            print(str(error))
        except (ValueError, KeyError, OSError, StopIteration):
            print('Operation stopped: invalid/missing local configuration. No sensitive values logged.')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local Resy reservation client')
    parser.add_argument('--dry-run', action='store_true', help='One availability pass; never enters checkout')
    parser.add_argument('--check', action='store_true', help='Validate saved task formats offline')
    parser.add_argument('--duration', type=float, default=120)
    args = parser.parse_args(argv)
    try:
        with client_lock():
            if args.check:
                tasks = load_data('tasks.json', [])
                for task in tasks:
                    reservation_dates(task['start_date'], task['end_date'])
                print(f'Validated date ranges for {len(tasks)} task(s); credentials were not tested.')
            elif args.dry_run:
                start_tasks(True, args.duration)
                for entry in list(MANAGER.running.values()):
                    entry['thread'].join()
            else:
                menu()
    except BlockingIOError:
        print('Another client is using this configuration directory; refusing a duplicate launch.')
    except KeyboardInterrupt:
        print('Stopping workers...')
    finally:
        MANAGER.close()


if __name__ == '__main__':
    main()

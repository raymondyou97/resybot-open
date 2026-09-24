"""Public reservation targets joined to private runtime settings, never credentials in Git."""

import configparser
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from client.config_store import load_data, save_data
from client.fees import validate_policy
from client.task_executor import reservation_dates
from client.time_window import time_window


PLAN_PATH = Path(__file__).resolve().parents[1] / 'reservations.txt'
SETTINGS_FILE = 'reservation-settings.json'
POLICY_FIELDS = ('accept_terms', 'currency', 'max_total_charge', 'max_cancellation_fee')
REQUIRED = {'url', 'venue_id', 'party_size', 'start_date', 'end_date', 'start_time', 'end_time', 'timezone'}
ALLOWED = REQUIRED | {'name', 'poll_interval_ms', 'campaign_id'}


def validate_url(value):
    url = urlsplit(value)
    if (
        url.scheme != 'https'
        or url.netloc != 'resy.com'
        or url.fragment
        or not re.fullmatch(r'/cities/[a-z0-9-]+/venues/[a-z0-9-]+/?', url.path)
    ):
        raise ValueError
    query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
    if set(query) - {'date', 'seats'} or any(len(values) != 1 for values in query.values()):
        raise ValueError
    if 'date' in query and date.fromisoformat(query['date'][0]).isoformat() != query['date'][0]:
        raise ValueError
    if 'seats' in query and not 1 <= int(query['seats'][0]) <= 20:
        raise ValueError


def load_goals():
    parser = configparser.ConfigParser(interpolation=None)
    try:
        if PLAN_PATH.is_symlink():
            raise ValueError
        with PLAN_PATH.open(encoding='utf-8') as stream:
            parser.read_file(stream)
        if parser.defaults():
            raise ValueError
        goals = []
        for goal_id in parser.sections():
            if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', goal_id):
                raise ValueError
            values = dict(parser[goal_id])
            if set(values) - ALLOWED or not REQUIRED <= set(values):
                raise ValueError
            validate_url(values['url'])
            if not re.fullmatch(r'[0-9]+', values['venue_id']) or int(values['venue_id']) <= 0:
                raise ValueError
            party = int(values['party_size'])
            delay = int(values.get('poll_interval_ms', '60000'))
            if not 1 <= party <= 20 or not 1000 <= delay <= 86400000:
                raise ValueError
            for key in ('start_time', 'end_time'):
                if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', values[key]):
                    raise ValueError
            reservation_dates(values['start_date'], values['end_date'])
            ZoneInfo(values['timezone'])
            goal = {
                'goal_id': goal_id,
                'restaurant_name': values.get('name', goal_id),
                'reservation_url': values['url'],
                'restaurant_id': values['venue_id'],
                'party_sz': party,
                'start_date': values['start_date'],
                'end_date': values['end_date'],
                'start_time': values['start_time'],
                'end_time': values['end_time'],
                'timezone': values['timezone'],
                'delay': delay,
                'campaign_id': values.get('campaign_id', goal_id),
            }
            if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', goal['campaign_id']):
                raise ValueError
            time_window(goal)
            goals.append(goal)
        return goals
    except (configparser.Error, ValueError, OSError, ZoneInfoNotFoundError):
        raise ValueError(
            'Invalid reservations.txt; check its public fields, dates, URL, and HH:MM times.'
        ) from None


def goal_fingerprint(goal):
    return hashlib.sha256(json.dumps(goal, sort_keys=True).encode()).hexdigest()


def save_goal_settings(goal, account, policy):
    validate_policy({**goal, **{key: policy.get(key) for key in POLICY_FIELDS}})
    settings = load_data(SETTINGS_FILE, {})
    if not isinstance(settings, dict):
        raise ValueError('Invalid private reservation settings.')
    settings[goal['goal_id']] = {
        'account_name': account['account_name'],
        'target_id': goal_fingerprint(goal),
        **{key: policy[key] for key in POLICY_FIELDS},
    }
    save_data(SETTINGS_FILE, settings)


def load_tasks(*, bind_accounts=True):
    if PLAN_PATH.is_symlink():
        raise ValueError('Refusing a symlinked reservations.txt.')
    if not PLAN_PATH.exists():
        return load_data('tasks.json', [])
    goals = load_goals()
    if not bind_accounts or not goals:
        return goals
    settings = load_data(SETTINGS_FILE, {})
    accounts = load_data('accounts.json', [])
    if (
        not isinstance(settings, dict)
        or not isinstance(accounts, list)
        or any(not isinstance(a, dict) for a in accounts)
    ):
        raise ValueError('Invalid private account or reservation settings.')
    tasks = []
    for goal in goals:
        local = settings.get(goal['goal_id'], {})
        if not isinstance(local, dict):
            raise ValueError('Invalid private reservation settings.')
        name = local.get('account_name')
        matches = [a for a in accounts if a.get('account_name') == name] if name else accounts
        if len(matches) != 1:
            raise ValueError('Select an account for the reservation goal in Show tasks.')
        account = matches[0]
        if (
            not isinstance(account.get('account_name'), str)
            or not account['account_name']
            or not isinstance(account.get('auth_token'), str)
            or not account['auth_token'].strip()
            or not str(account.get('payment_id', '')).isdigit()
            or int(account['payment_id']) <= 0
        ):
            raise ValueError('The selected local account is incomplete.')
        task = {**goal, **{key: account[key] for key in ('account_name', 'auth_token', 'payment_id')}}
        task['accept_terms'] = False
        if local.get('target_id') == goal_fingerprint(goal):
            task.update({key: local.get(key) for key in POLICY_FIELDS})
        tasks.append(task)
    return tasks

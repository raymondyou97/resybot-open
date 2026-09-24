"""Persisted schedule specifications and interruptible worker lifecycle."""

import hashlib
import json
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from client.config_store import load_data, save_data
from client.control import RunControl
from client.task_executor import run_tasks_concurrently


TIMEZONE = ZoneInfo('America/New_York')


def task_id(task):
    identity = {
        key: task.get(key)
        for key in (
            'account_id',
            'account_name',
            'restaurant_id',
            'party_sz',
            'start_date',
            'end_date',
            'start_time',
            'end_time',
            'campaign_id',
        )
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def trigger_for(spec, now=None):
    now = now or datetime.now(TIMEZONE)
    hour, minute = map(int, spec['time'].split(':'))
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError('Invalid schedule time.')
    if spec['repeat'] == 'Once':
        when = datetime.fromisoformat(spec['at'])
        if when.tzinfo is None:
            raise ValueError('One-time schedules require an explicit timezone.')
        return 'date', {'run_date': when, 'timezone': TIMEZONE}
    if spec['repeat'] not in {'Daily', 'Weekly'}:
        raise ValueError('Unknown recurrence.')
    options = {'hour': hour, 'minute': minute, 'timezone': TIMEZONE}
    if spec['repeat'] == 'Weekly':
        if spec.get('weekday') not in {'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'}:
            raise ValueError('Weekly schedules require exactly one weekday.')
        options['day_of_week'] = spec['weekday']
    return CronTrigger(**options), {}


def next_once(clock, now=None):
    now = now or datetime.now(TIMEZONE)
    hour, minute = map(int, clock.split(':'))
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= now:
        when += timedelta(days=1)
    return when.isoformat()


class TaskManager:
    def __init__(self, worker=run_tasks_concurrently):
        self.worker = worker
        self.running = {}
        self.lock = threading.Lock()
        self.spec_lock = threading.RLock()
        self.scheduler = None

    def start(self, key, tasks, proxies, info, duration=120, dry_run=True):
        with self.lock:
            if key in self.running and self.running[key]['thread'].is_alive():
                return False
            control = RunControl(duration)

            def run():
                try:
                    self.worker(
                        tasks,
                        '',
                        '',
                        proxies,
                        info.get('discord_webhook', ''),
                        control=control,
                        dry_run=dry_run,
                    )
                except Exception:
                    print('Worker stopped due to a local error; no sensitive details logged.')

            thread = threading.Thread(target=run, name=f'resy-task-{key[:12]}')
            self.running[key] = {'thread': thread, 'control': control}
            thread.start()
            return True

    def stop(self, key, timeout=0.2):
        with self.lock:
            entry = self.running.get(key)
        if not entry:
            return True
        entry['control'].stop()
        entry['thread'].join(timeout)
        finished = not entry['thread'].is_alive()
        if finished:
            with self.lock:
                if self.running.get(key) is entry:
                    self.running.pop(key)
        return finished

    def statuses(self):
        with self.lock:
            return {
                key: ('stopping' if entry['control'].stopped() else 'running')
                if entry['thread'].is_alive()
                else 'finished'
                for key, entry in self.running.items()
            }

    def dispatch(self, spec):
        # Consume one-shot jobs before starting; a crash cannot repeat a submission.
        if spec['repeat'] == 'Once':
            self.remove_spec(spec['id'])
        tasks = load_data('tasks.json', [])
        task = next((task for task in tasks if task_id(task) == spec['task_id']), None)
        if task is None:
            print('Scheduled task no longer exists; skipped without substituting another task.')
            return
        self.start(
            spec['task_id'],
            [task],
            load_data('proxies.json', []),
            load_data('info.json', {}),
            duration=spec['duration'],
            dry_run=spec.get('dry_run', True),
        )

    def ensure_scheduler(self):
        if self.scheduler is None:
            self.scheduler = BackgroundScheduler(timezone=TIMEZONE)
            self.scheduler.start()
            for spec in load_data('schedules.json', []):
                if spec['repeat'] == 'Once' and datetime.fromisoformat(spec['at']) <= datetime.now(TIMEZONE):
                    continue
                self.install(spec)
        return self.scheduler

    def install(self, spec):
        trigger, kwargs = trigger_for(spec)
        self.scheduler.add_job(
            self.dispatch,
            trigger,
            args=[spec],
            id=spec['id'],
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=1,
            **kwargs,
        )

    def add(self, spec):
        RunControl(spec['duration'])
        trigger_for(spec)
        self.ensure_scheduler()
        with self.spec_lock:
            specs = load_data('schedules.json', [])
            save_data('schedules.json', [item for item in specs if item['id'] != spec['id']] + [spec])
            self.install(spec)

    def remove_spec(self, key):
        with self.spec_lock:
            specs = load_data('schedules.json', [])
            save_data('schedules.json', [spec for spec in specs if spec['id'] != key])

    def remove(self, key):
        self.remove_spec(key)
        if self.scheduler and self.scheduler.get_job(key):
            self.scheduler.remove_job(key)

    def close(self):
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        with self.lock:
            keys = list(self.running)
            for entry in self.running.values():
                entry['control'].stop()
        for key in keys:
            self.stop(key, timeout=12)

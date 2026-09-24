"""Cross-process claims and durable confirmation/uncertainty holds."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from client.config_store import data_path


class CampaignBlocked(Exception):
    pass


def account_key(task):
    identity = task.get('account_id') or task.get('account_name')
    if not identity:
        raise ValueError('A stable account name or account ID is required.')
    return hashlib.sha256(str(identity).encode()).hexdigest()


def campaign_key(task):
    return str(task.get('campaign_id') or task['restaurant_id'])


class BookingState:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_path('.state/bookings.sqlite3')
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        if self.path.is_symlink():
            raise ValueError('Refusing symlinked booking state.')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("""CREATE TABLE IF NOT EXISTS claims (
                id TEXT PRIMARY KEY, account TEXT NOT NULL, campaign TEXT NOT NULL,
                venue TEXT NOT NULL, day TEXT NOT NULL, clock TEXT NOT NULL,
                party INTEGER NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                reservation_ref TEXT, quote_ref TEXT, policy TEXT
            )""")
            columns = {row['name'] for row in db.execute('PRAGMA table_info(claims)')}
            for name in ('quote_ref', 'policy'):
                if name not in columns:
                    db.execute(f'ALTER TABLE claims ADD COLUMN {name} TEXT')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def blocked(self, task):
        with self.connect() as db:
            return (
                db.execute(
                    """SELECT 1 FROM claims WHERE account=?
                AND (campaign=? OR venue=?) AND status != 'released' LIMIT 1""",
                    (account_key(task), campaign_key(task), str(task['restaurant_id'])),
                ).fetchone()
                is not None
            )

    def claim(self, task, day, clock):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute(
                """SELECT 1 FROM claims WHERE account=?
                AND (campaign=? OR venue=?) AND status != 'released' LIMIT 1""",
                (account_key(task), campaign_key(task), str(task['restaurant_id'])),
            ).fetchone()
            if existing:
                raise CampaignBlocked(
                    'A prior attempt needs verification or this campaign already succeeded.'
                )
            claim_id = uuid.uuid4().hex
            db.execute(
                'INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    claim_id,
                    account_key(task),
                    campaign_key(task),
                    str(task['restaurant_id']),
                    day,
                    clock,
                    task['party_sz'],
                    'claimed',
                    time.time(),
                    None,
                    None,
                    json.dumps(
                        {
                            key: task.get(key)
                            for key in (
                                'accept_terms',
                                'currency',
                                'max_total_charge',
                                'max_cancellation_fee',
                                'party_sz',
                            )
                        }
                    ),
                ),
            )
        return claim_id

    def transition(self, claim_id, old, new, reservation_ref=None):
        with self.connect() as db:
            changed = db.execute(
                """UPDATE claims SET status=?, reservation_ref=COALESCE(?, reservation_ref)
                WHERE id=? AND status=?""",
                (new, reservation_ref, claim_id, old),
            ).rowcount
            if changed != 1:
                raise CampaignBlocked('Claim state changed; inspect it before another action.')

    def bind_quote(self, claim_id, book_token):
        digest = hashlib.sha256(book_token.encode()).hexdigest()
        with self.connect() as db:
            changed = db.execute(
                "UPDATE claims SET quote_ref=? WHERE id=? AND status='claimed'", (digest, claim_id)
            ).rowcount
            if changed != 1:
                raise CampaignBlocked('Quote cannot be attached to this claim.')

    def dispatch(self, claim_id, book_token):
        digest = hashlib.sha256(book_token.encode()).hexdigest()
        with self.connect() as db:
            changed = db.execute(
                """UPDATE claims SET status='dispatching'
                WHERE id=? AND status='submitted' AND quote_ref=?""",
                (claim_id, digest),
            ).rowcount
            if changed != 1:
                raise CampaignBlocked('Submission quote mismatch, duplicate, or claim not ready.')

    def submitted(self, claim_id):
        self.transition(claim_id, 'claimed', 'submitted')

    def release_unsubmitted(self, claim_id):
        self.transition(claim_id, 'claimed', 'released')

    def confirmed(self, claim_id, reference):
        with self.connect() as db:
            changed = db.execute(
                """UPDATE claims SET status='confirmed', reservation_ref=?
                WHERE id=? AND status IN ('submitted', 'dispatching')""",
                (reference, claim_id),
            ).rowcount
            if changed != 1:
                raise CampaignBlocked('Only a submitted attempt can be confirmed.')

    def rows(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM claims ORDER BY created')]

    def get(self, claim_id):
        return next((row for row in self.rows() if row['id'] == claim_id), None)


def reservation_reference(row):
    reference = row.get('reservation_id')
    if reference is None:
        reference = row.get('id')
    if not ((isinstance(reference, str) and reference.strip()) or (type(reference) is int and reference > 0)):
        raise ValueError('Reservation response has no stable reference.')
    return hashlib.sha256(json.dumps(reference, sort_keys=True).encode()).hexdigest()

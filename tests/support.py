"""All tests use synthetic accounts and prohibit real socket connections."""

import socket
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from client.booking_state import BookingState


def no_network(*args, **kwargs):
    raise AssertionError('Real network access is forbidden in this test suite.')


socket.socket.connect = no_network
socket.socket.connect_ex = no_network


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_patch = patch('client.config_store.DATA_DIR', self.root)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.plan_patch = patch('client.reservation_plan.PLAN_PATH', self.root / 'reservations.txt')
        self.plan_patch.start()
        self.addCleanup(self.plan_patch.stop)
        self.state = BookingState(self.root / '.state/bookings.sqlite3')


def task(**overrides):
    return {
        'account_name': 'offline-account',
        'auth_token': 'fixture-account-token',
        'payment_id': 1,
        'restaurant_id': '123',
        'party_sz': 2,
        'start_date': '2099-01-01',
        'end_date': '2099-01-01',
        'start_time': 18,
        'end_time': 19,
        'delay': 1000,
        'accept_terms': True,
        'max_total_charge': '5',
        'max_cancellation_fee': '10',
        'currency': 'USD',
        **overrides,
    }


def quote(total=5, cancellation=5, currency='USD'):
    return {
        'response_value': 'fixture-book-token',
        'details': {
            'payment': {'config': {'currency': currency}, 'amounts': {'total': total}},
            'cancellation': {'fee': {'amount': cancellation}},
        },
    }


def reservation(**overrides):
    return {
        'venue': {'id': 123},
        'day': '2099-01-01',
        'time_slot': '18:30:00',
        'num_seats': 2,
        'reservation_id': 12345,
        'resy_token': 'fixture-reservation-token',
        'cancellation': {'fee': {'amount': 0}},
        **overrides,
    }


def availability(slots=None, name='Dear Margo', venue_id=123):
    if slots is None:
        slots = [{'date': {'start': '2099-01-01 18:30:00'}, 'config': {'token': 'fixture-slot-token'}}]
    return {'results': {'venues': [{'venue': {'id': {'resy': venue_id}, 'name': name}, 'slots': slots}]}}

from concurrent.futures import ThreadPoolExecutor
import io
import json
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from client.booking_state import BookingState, CampaignBlocked, reservation_reference
from client.config_store import load_data, save_data
from client.fees import FeePolicyError, validate_quote
from client.local_auth import local_token
from client.reservations import cancel_verified, list_account, resolve_claim
from client.verification import VerificationError
from support import OfflineTest, quote, reservation, task


class StateTests(OfflineTest):
    def test_concurrent_claims_only_one_wins(self):
        def claim(_):
            try:
                return BookingState(self.state.path).claim(task(), '2099-01-01', '18:30')
            except CampaignBlocked:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(8)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_holds_survive_restarts_and_block_alternatives(self):
        key = self.state.claim(task(campaign_id='dinner'), '2099-01-01', '18:30')
        self.state.submitted(key)
        restarted = BookingState(self.state.path)
        self.assertTrue(restarted.blocked(task(restaurant_id='456', campaign_id='dinner')))
        self.assertFalse(restarted.blocked(task(account_name='second-account')))
        with self.assertRaises(CampaignBlocked):
            restarted.release_unsubmitted(key)

    def test_dispatch_is_exactly_once(self):
        key = self.state.claim(task(), '2099-01-01', '18:30')
        self.state.submitted(key)
        self.state.transition(key, 'submitted', 'dispatching')
        with self.assertRaises(CampaignBlocked):
            self.state.transition(key, 'submitted', 'dispatching')
        self.state.confirmed(key, 'hashed-reference')
        self.assertTrue(self.state.blocked(task()))

    def test_state_contains_no_credentials(self):
        self.state.claim(task(), '2099-01-01', '18:30')
        serialized = json.dumps(self.state.rows())
        self.assertNotIn(task()['auth_token'], serialized)
        self.assertNotIn(task()['account_name'], serialized)
        self.assertEqual(self.state.path.stat().st_mode & 0o777, 0o600)

    def test_unverified_hold_cannot_be_automatically_released(self):
        key = self.state.claim(task(), '2099-01-01', '18:30')
        self.state.submitted(key)
        with patch('client.reservations.upcoming', return_value={'reservations': []}):
            with self.assertRaises(VerificationError):
                resolve_claim(self.state, key, task())
        self.assertEqual(self.state.get(key)['status'], 'submitted')

    def test_cancellation_reconciliation_does_not_ignore_changed_details(self):
        key = self.state.claim(task(), '2099-01-01', '18:30')
        self.state.submitted(key)
        reference = reservation_reference(reservation())
        self.state.confirmed(key, reference)
        self.state.transition(key, 'confirmed', 'cancellation-pending')
        with patch(
            'client.reservations.upcoming', return_value={'reservations': [reservation(time_slot='19:30:00')]}
        ):
            with self.assertRaises(VerificationError):
                resolve_claim(self.state, key, task())
        self.assertEqual(self.state.get(key)['status'], 'cancellation-pending')

    def test_release_does_not_ignore_different_reservation_at_same_venue(self):
        key = self.state.claim(task(), '2099-01-01', '18:30')
        self.state.submitted(key)
        with patch(
            'client.reservations.upcoming', return_value={'reservations': [reservation(time_slot='19:30:00')]}
        ):
            with self.assertRaises(VerificationError):
                resolve_claim(self.state, key, task(), checked_no_booking=True)
        self.assertEqual(self.state.get(key)['status'], 'submitted')


class StorageTests(OfflineTest):
    def test_atomic_private_json_and_cwd_independence(self):
        save_data('accounts.json', [{'auth_token': 'fixture'}])
        self.assertEqual(load_data('accounts.json', []), [{'auth_token': 'fixture'}])
        self.assertEqual((self.root / 'accounts.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.glob('.config-*')), [])

    def test_invalid_json_and_symlink_fail_closed(self):
        (self.root / 'bad.json').write_text('private-invalid-content')
        with self.assertRaisesRegex(ValueError, 'Configuration could not be read'):
            load_data('bad.json', {})
        (self.root / 'link.json').symlink_to(self.root / 'bad.json')
        with self.assertRaises(ValueError):
            save_data('link.json', {})
        self.assertEqual((self.root / 'bad.json').read_text(), 'private-invalid-content')

    def test_local_token_is_private_and_reused(self):
        path = self.root / '.state/.local-server-token'
        first = local_token(path)
        self.assertGreaterEqual(len(first), 32)
        self.assertEqual(first, local_token(path))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class FeeTests(OfflineTest):
    def test_valid_quote(self):
        validate_quote(task(), quote()['details'])

    def test_explicit_any_approval_removes_only_the_selected_ceiling(self):
        validate_quote(
            task(max_total_charge='any', max_cancellation_fee='any'),
            quote(total=500, cancellation=500)['details'],
        )
        with self.assertRaises(FeePolicyError):
            validate_quote(task(max_total_charge='any'), quote(total=500, cancellation=500)['details'])
        with self.assertRaises(FeePolicyError):
            validate_quote(task(max_cancellation_fee='any'), quote(total=500, cancellation=500)['details'])

    def test_any_fee_approval_does_not_allow_invalid_quotes_or_implicit_approval(self):
        for details in ({}, quote(total=None)['details'], quote(currency='JPY')['details']):
            with self.assertRaises(FeePolicyError):
                validate_quote(task(max_total_charge='any', max_cancellation_fee='any'), details)
        for value in (None, '', 'unlimited', 'Infinity', True):
            with self.assertRaises(FeePolicyError):
                validate_quote(task(max_total_charge=value), quote()['details'])

    def test_missing_unknown_excessive_or_unaccepted_terms_stop(self):
        cases = [
            (task(), {}),
            (task(), quote(total=6)['details']),
            (task(), quote(cancellation=11)['details']),
            (task(), quote(currency=None)['details']),
            (task(), quote(currency='JPY')['details']),
            (task(accept_terms=False), quote()['details']),
            (task(max_total_charge='NaN'), quote()['details']),
            (task(), quote(total=True)['details']),
            (task(), quote(total=-1)['details']),
        ]
        for config, details in cases:
            with self.subTest(config=config.get('accept_terms')), self.assertRaises(FeePolicyError):
                validate_quote(config, details)


class CancellationTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.account = task()
        self.raw = reservation()
        self.record = {
            'reference': reservation_reference(self.raw),
            'day': '2099-01-01',
            'time_slot': '18:30',
            'venue_id': '123',
            'num_seats': 2,
        }
        save_data('resrevations.json', [self.record])

    def test_verified_cancellation_removes_record(self):
        with (
            patch(
                'client.reservations.upcoming',
                side_effect=[{'reservations': [self.raw]}, {'reservations': []}],
            ),
            patch('client.reservations.requests.post', return_value=Mock(status_code=200)) as post,
        ):
            self.assertTrue(cancel_verified(self.account, self.record, state=self.state))
            post.assert_called_once()
        self.assertEqual(load_data('resrevations.json', []), [])

    def test_failed_or_unverified_cancellation_keeps_record(self):
        for status, still_present in [(500, False), (200, True)]:
            save_data('resrevations.json', [self.record])
            with (
                patch(
                    'client.reservations.upcoming',
                    side_effect=[
                        {'reservations': [self.raw]},
                        {'reservations': [self.raw] if still_present else []},
                    ],
                ),
                patch('client.reservations.requests.post', return_value=Mock(status_code=status)),
            ):
                with self.assertRaises(VerificationError):
                    cancel_verified(self.account, self.record, state=self.state)
            self.assertEqual(len(load_data('resrevations.json', [])), 1)
            self.assertTrue(load_data('resrevations.json', [])[0]['cancellation_pending'])

    def test_explicit_nonapplicable_fee_allows_free_cancellation(self):
        current = reservation(cancellation={'allowed': True, 'fee': {'amount': None, 'applies': False}})
        with (
            patch(
                'client.reservations.upcoming',
                side_effect=[{'reservations': [current]}, {'reservations': []}],
            ),
            patch('client.reservations.requests.post', return_value=Mock(status_code=200)) as post,
        ):
            self.assertTrue(cancel_verified(self.account, self.record, state=self.state))
        post.assert_called_once()

    def test_rotating_token_does_not_falsely_prove_cancellation(self):
        refreshed = reservation(resy_token='fixture-refreshed-token')
        with (
            patch(
                'client.reservations.upcoming',
                side_effect=[{'reservations': [self.raw]}, {'reservations': [refreshed]}],
            ),
            patch('client.reservations.requests.post', return_value=Mock(status_code=200)),
            self.assertRaises(VerificationError),
        ):
            cancel_verified(self.account, self.record, state=self.state)
        self.assertTrue(load_data('resrevations.json', [])[0]['cancellation_pending'])

    def test_null_fee_without_explicit_waiver_never_submits(self):
        for applies in (None, True, 0, 'false'):
            current = reservation(cancellation={'fee': {'amount': None, 'applies': applies}})
            with (
                patch('client.reservations.upcoming', return_value={'reservations': [current]}),
                patch('client.reservations.requests.post') as post,
                self.assertRaises(FeePolicyError),
            ):
                cancel_verified(self.account, self.record, state=self.state)
            post.assert_not_called()

    def test_unknown_cancellation_fee_never_submits(self):
        raw = reservation(cancellation={})
        with (
            patch('client.reservations.upcoming', return_value={'reservations': [raw]}),
            patch('client.reservations.requests.post') as post,
        ):
            with self.assertRaises(FeePolicyError):
                cancel_verified(self.account, self.record, state=self.state)
            post.assert_not_called()

    def test_multiple_reservations_parse_without_shadowing_response(self):
        data = {
            'reservations': [
                reservation(),
                reservation(reservation_id=67890, resy_token='second-fixture', venue={'id': 456}),
            ],
            'venues': {'123': {'name': 'First'}, '456': {'name': 'Second'}},
        }
        with patch('client.reservations.upcoming', return_value=data), redirect_stdout(io.StringIO()):
            records = list_account(self.account)
        self.assertEqual([row['venue'] for row in records], ['First', 'Second'])
        self.assertNotIn('auth_token', json.dumps(records))
        self.assertNotIn('fixture-reservation-token', json.dumps(records))

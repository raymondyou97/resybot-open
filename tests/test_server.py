from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from server.server import create_app
from support import OfflineTest, task


class LocalServerTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.token = 'fixture-local-bearer-value-not-a-real-secret'
        self.app = create_app(self.token, self.state)
        self.client = TestClient(self.app, base_url='http://127.0.0.1', raise_server_exceptions=False)
        self.headers = {'Authorization': f'Bearer {self.token}'}

    def test_health_is_public_but_sensitive_routes_require_auth(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.post('/api/get-details', json={}).status_code, 401)
        self.assertEqual(self.client.post('/api/get-details', json={}, headers=self.headers).status_code, 422)

    def test_browser_origin_and_untrusted_host_rejected(self):
        response = self.client.post(
            '/api/get-details', headers={**self.headers, 'Origin': 'https://example.invalid'}, json={}
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.get('/', headers={'Host': 'attacker.invalid'})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('access-control-allow-origin', response.headers)

    def test_validation_errors_do_not_echo_secrets(self):
        response = self.client.post(
            '/api/get-details', headers=self.headers, json={'party_size': 'fixture-private-value'}
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('fixture-private-value', response.text)

    def test_request_size_is_bounded(self):
        response = self.client.post('/api/get-details', headers=self.headers, content='x' * 32769)
        self.assertEqual(response.status_code, 413)

    def test_booking_requires_matching_claim_and_dispatches_once(self):
        key = self.state.claim(task(), '2099-01-01', '18:30')
        payload = {
            'book_token': 'fixture',
            'payment_id': 1,
            'day': '2099-01-01',
            'party_size': 2,
            'restaurant_id': '123',
            'claim_id': key,
            'headers': {'Authorization': 'fixture', 'X-Resy-Auth-Token': 'fixture'},
        }
        with patch('server.server.httpx.AsyncClient') as cls:
            upstream = AsyncMock()
            upstream.post.return_value = Mock(status_code=201)
            cls.return_value.__aenter__.return_value = upstream
            # A merely claimed attempt cannot be dispatched.
            self.assertEqual(
                self.client.post('/api/book-reservation', json=payload, headers=self.headers).status_code, 409
            )
            upstream.post.assert_not_called()
            self.state.bind_quote(key, 'fixture')
            self.state.submitted(key)
            response = self.client.post('/api/book-reservation', json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 201)
            self.assertFalse(response.json()['verified'])
            self.assertEqual(self.state.get(key)['status'], 'dispatching')
            response = self.client.post('/api/book-reservation', json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 409)
            upstream.post.assert_called_once()
            self.assertEqual(
                upstream.post.call_args.kwargs['headers']['User-Agent'],
                'resybot-open/1.0 (personal reservation client)',
            )

    def test_booking_cannot_substitute_a_different_quote(self):
        key = self.state.claim(task(), '2099-01-01', '18:30')
        self.state.bind_quote(key, 'approved-fixture')
        self.state.submitted(key)
        payload = {
            'book_token': 'different-fixture',
            'payment_id': 1,
            'day': '2099-01-01',
            'party_size': 2,
            'restaurant_id': '123',
            'claim_id': key,
            'headers': {'Authorization': 'fixture', 'X-Resy-Auth-Token': 'fixture'},
        }
        with patch('server.server.httpx.AsyncClient') as client:
            result = self.client.post('/api/book-reservation', json=payload, headers=self.headers)
            self.assertEqual(result.status_code, 409)
            client.assert_not_called()
        self.assertEqual(self.state.get(key)['status'], 'submitted')

    def test_upstream_booking_exception_retains_hold_and_hides_url(self):
        import httpx

        key = self.state.claim(task(), '2099-01-01', '18:30')
        self.state.bind_quote(key, 'fixture')
        self.state.submitted(key)
        payload = {
            'book_token': 'fixture',
            'payment_id': 1,
            'day': '2099-01-01',
            'party_size': 2,
            'restaurant_id': '123',
            'claim_id': key,
            'headers': {'Authorization': 'fixture', 'X-Resy-Auth-Token': 'fixture'},
        }
        with patch('server.server.httpx.AsyncClient') as cls:
            upstream = AsyncMock()
            upstream.post.side_effect = httpx.ReadTimeout('fixture-private-url')
            cls.return_value.__aenter__.return_value = upstream
            response = self.client.post('/api/book-reservation', json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('fixture-private-url', response.text)
        self.assertEqual(self.state.get(key)['status'], 'dispatching')

    def test_details_omit_account_and_payment_method_data(self):
        key = self.state.claim(task(), '2099-01-01', '18:30')
        payload = {
            'claim_id': key,
            'day': '2099-01-01',
            'party_size': 2,
            'config_token': 'fixture',
            'restaurant_id': '123',
            'headers': {'Authorization': 'fixture', 'X-Resy-Auth-Token': 'fixture'},
        }
        with patch('server.server.httpx.AsyncClient') as cls:
            upstream = AsyncMock()
            upstream.get.return_value = Mock(
                status_code=200,
                json=lambda: {
                    'book_token': {'value': 'fixture-book'},
                    'user': {'email': 'private@example.invalid'},
                    'payment': {
                        'amounts': {'total': 0},
                        'config': {'currency': 'USD'},
                        'methods': ['private-method'],
                    },
                    'cancellation': {'fee': {'amount': 0}},
                },
            )
            cls.return_value.__aenter__.return_value = upstream
            response = self.client.post('/api/get-details', json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            upstream.get.call_args.kwargs['headers']['User-Agent'],
            'resybot-open/1.0 (personal reservation client)',
        )
        self.assertNotIn('private@example.invalid', response.text)
        self.assertNotIn('private-method', response.text)
        self.assertEqual(response.json()['details']['payment']['amounts']['total'], 0)

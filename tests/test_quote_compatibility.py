from copy import deepcopy

from client.fees import FeePolicyError, quote_summary, validate_quote
from support import OfflineTest, task


def free_quote():
    return {
        'payment': {'config': {'type': 'free'}, 'amounts': {'total': 0.0}},
        'cancellation': {'fee': None},
    }


class QuoteCompatibilityTests(OfflineTest):
    def test_explicit_free_quote_does_not_require_currency_for_zero_amounts(self):
        details = quote_summary(free_quote())
        self.assertIsNone(details['cancellation']['fee'])
        validate_quote(task(max_total_charge='0', max_cancellation_fee='0'), details)

    def test_missing_fee_is_not_the_same_as_explicit_null(self):
        raw = free_quote()
        del raw['cancellation']['fee']
        with self.assertRaises(FeePolicyError):
            quote_summary(raw)
        with self.assertRaises(FeePolicyError):
            validate_quote(task(), raw)

    def test_free_label_cannot_hide_charges_or_conflicting_currency(self):
        for total, fee, currency in [
            (1, None, None),
            (0, {'amount': 1}, None),
            (0, None, 'JPY'),
            (0, None, ''),
        ]:
            raw = free_quote()
            raw['payment']['amounts']['total'] = total
            raw['cancellation']['fee'] = fee
            raw['payment']['config']['currency'] = currency
            with self.subTest(total=total, currency=currency), self.assertRaises(FeePolicyError):
                validate_quote(task(max_total_charge='0', max_cancellation_fee='0'), quote_summary(raw))

    def test_null_fee_needs_explicit_free_type_and_explicit_total(self):
        variants = []
        for kind in (None, '', 'paid', 'deposit'):
            raw = free_quote()
            raw['payment']['config']['type'] = kind
            raw['payment']['config']['currency'] = 'USD'
            variants.append(raw)
        for total in (None, True, 'NaN', -1):
            raw = free_quote()
            raw['payment']['amounts']['total'] = total
            variants.append(raw)
        raw = free_quote()
        del raw['payment']['amounts']['total']
        variants.append(raw)
        for index, raw in enumerate(variants):
            with self.subTest(case=index), self.assertRaises(FeePolicyError):
                validate_quote(task(), quote_summary(raw))

    def test_summary_never_returns_unneeded_account_fields(self):
        raw = deepcopy(free_quote())
        raw['user'] = {'email': 'fixture@example.invalid'}
        raw['book_token'] = {'value': 'fixture-book-token'}
        raw['payment']['methods'] = ['fixture-payment-method']
        details = quote_summary(raw)
        self.assertEqual(set(details), {'payment', 'cancellation', 'currency'})
        self.assertEqual(set(details['payment']), {'amounts', 'config'})

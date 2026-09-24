"""Fail-closed charge ceilings for explicitly approved live tasks."""

from decimal import Decimal, InvalidOperation


class FeePolicyError(ValueError):
    pass


def amount(value):
    if isinstance(value, bool) or value is None:
        raise FeePolicyError('A required monetary amount is missing or invalid.')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise FeePolicyError('A required monetary amount is invalid.') from None
    if not result.is_finite() or result < 0:
        raise FeePolicyError('A required monetary amount is invalid.')
    return result


def fee_limit(value):
    """Only an explicit 'any' approval removes a ceiling; missing values never do."""
    return None if value == 'any' else amount(value)


def validate_policy(task):
    if task.get('accept_terms') is not True:
        raise FeePolicyError('Live tasks require explicit policy acceptance in task configuration.')
    party = task.get('party_sz')
    if isinstance(party, bool) or not isinstance(party, int) or not 1 <= party <= 20:
        raise FeePolicyError('A valid party size is required for charge limits.')
    for key in ('max_total_charge', 'max_cancellation_fee'):
        fee_limit(task.get(key))
    currency = task.get('currency')
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isupper():
        raise FeePolicyError('Live tasks require an explicit three-letter currency.')


def quote_summary(raw):
    """Keep only fee fields, preserving absent fields versus explicit no-fee values."""
    try:
        payment = raw['payment']
        config = payment['config']
        cancellation = raw['cancellation']
        if not all(isinstance(value, dict) for value in (payment, config, cancellation)):
            raise TypeError
        fee = cancellation['fee']
        if fee is not None and not isinstance(fee, dict):
            raise TypeError
        return {
            'payment': {
                'amounts': {'total': payment['amounts']['total']},
                'config': {'currency': config.get('currency'), 'type': config.get('type')},
            },
            'cancellation': {'fee': None if fee is None else {'amount': fee['amount']}},
            'currency': raw.get('currency'),
        }
    except (KeyError, TypeError):
        raise FeePolicyError('Quote lacks verified charge/cancellation fields; not submitting.') from None


def validate_quote(task, details):
    validate_policy(task)
    try:
        payment = details['payment']
        config = payment['config']
        currency = config.get('currency')
        if currency is None:
            currency = details.get('currency')
        total = amount(payment['amounts']['total'])
        explicitly_free = config.get('type') == 'free' and total == 0
        fee = details['cancellation']['fee']
        if fee is None:
            if not explicitly_free:
                raise FeePolicyError('A null cancellation fee requires an explicitly free, zero-total quote.')
            cancellation = amount(0)
        else:
            cancellation = amount(fee['amount']) * task['party_sz']
    except (KeyError, TypeError, AttributeError):
        raise FeePolicyError('Quote lacks verified charge/cancellation fields; not submitting.') from None
    if currency != task['currency']:
        if not (currency is None and explicitly_free and cancellation == 0):
            raise FeePolicyError('Quote currency is missing or differs from the task; not submitting.')
    total_limit = fee_limit(task['max_total_charge'])
    cancellation_limit = fee_limit(task['max_cancellation_fee'])
    if total_limit is not None and total > total_limit:
        raise FeePolicyError('Total charge exceeds the approved ceiling; not submitting.')
    if cancellation_limit is not None and cancellation > cancellation_limit:
        raise FeePolicyError('Cancellation fee exceeds the approved ceiling; not submitting.')

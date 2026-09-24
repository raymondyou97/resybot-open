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


def validate_policy(task):
    if task.get('accept_terms') is not True:
        raise FeePolicyError('Live tasks require explicit policy acceptance in task configuration.')
    party = task.get('party_sz')
    if isinstance(party, bool) or not isinstance(party, int) or not 1 <= party <= 20:
        raise FeePolicyError('A valid party size is required for charge limits.')
    for key in ('max_total_charge', 'max_cancellation_fee'):
        amount(task.get(key))
    currency = task.get('currency')
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isupper():
        raise FeePolicyError('Live tasks require an explicit three-letter currency.')


def validate_quote(task, details):
    validate_policy(task)
    try:
        payment = details['payment']
        currency = payment['config'].get('currency') or details.get('currency')
        total = amount(payment['amounts']['total'])
        cancellation = amount(details['cancellation']['fee']['amount']) * task['party_sz']
    except (KeyError, TypeError):
        raise FeePolicyError('Quote lacks verified charge/cancellation fields; not submitting.') from None
    if currency != task['currency']:
        raise FeePolicyError('Quote currency is missing or differs from the task; not submitting.')
    if total > amount(task['max_total_charge']):
        raise FeePolicyError('Total charge exceeds the approved ceiling; not submitting.')
    if cancellation > amount(task['max_cancellation_fee']):
        raise FeePolicyError('Cancellation fee exceeds the approved ceiling; not submitting.')

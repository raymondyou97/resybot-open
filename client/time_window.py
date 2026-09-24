"""Exact minute windows, with backward-compatible legacy integer hours."""

import re


def minute_value(value, *, end=False):
    if isinstance(value, str) and re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
        hour, minute = map(int, value.split(':'))
        return hour * 60 + minute
    if type(value) is int or (isinstance(value, str) and re.fullmatch(r'[0-9]{1,2}', value)):
        hour = int(value)
        if 0 <= hour <= 23:
            return hour * 60 + (59 if end else 0)
    raise ValueError('Times must be HH:MM or legacy integer hours between 0 and 23.')


def time_window(task):
    start = minute_value(task['start_time'])
    end = minute_value(task['end_time'], end=True)
    if start > end:
        raise ValueError('The reservation time window must not cross midnight.')
    return start, end


def display_window(task):
    start, end = time_window(task)
    return f'{start // 60:02d}:{start % 60:02d}–{end // 60:02d}:{end % 60:02d}'

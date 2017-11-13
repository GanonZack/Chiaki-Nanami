import collections
import datetime
import re
import parsedatetime as pdt

from dateutil.relativedelta import relativedelta
from discord.ext import commands

from .formats import human_join, pluralize


_short_time_pattern = re.compile("""
    (?:(?P<years>[0-9])(?:years?|y))?             # e.g. 2y
    (?:(?P<months>[0-9]{1,2})(?:months?|mo))?     # e.g. 2months
    (?:(?P<weeks>[0-9]{1,4})(?:weeks?|w))?        # e.g. 10w
    (?:(?P<days>[0-9]{1,5})(?:days?|d))?          # e.g. 14d
    (?:(?P<hours>[0-9]{1,5})(?:hours?|h))?        # e.g. 12h
    (?:(?P<minutes>[0-9]{1,5})(?:minutes?|m))?    # e.g. 10m
    (?:(?P<seconds>[0-9]{1,5})(?:seconds?|s))?    # e.g. 15s
""", re.VERBOSE)


DURATION_MULTIPLIERS = {
    'years'  : 60 * 60 * 24 * 365,
    'months' : 60 * 60 * 24 * 30,
    'weeks'  : 60 * 60 * 24 * 7,
    'days'   : 60 * 60 * 24,
    'hours'  : 60 * 60,
    'minutes': 60,
    'seconds': 1,
}


def _get_short_time_match(arg):
    match = _short_time_pattern.fullmatch(arg)
    if match is None or not match.group(0):
        raise commands.BadArgument('invalid time provided')
    return match


class Delta(collections.namedtuple('Delta', 'delta')):
    __slots__ = ()

    def __new__(cls, argument):
        match = _get_short_time_match(argument)
        data = {k: int(v) for k, v in match.groupdict(default=0).items()}
        return super().__new__(cls, relativedelta(**data))

    def __str__(self):
        return parse_delta(self.delta)

    @property
    def duration(self):
        attrs = ['years', 'months', 'days', 'hours', 'minutes', 'seconds']
        return sum(getattr(self.delta, attr, 0) * DURATION_MULTIPLIERS[attr] for attr in attrs)


# ----------------------- Time --------------------

_TimeBase = collections.namedtuple('Delta', 'dt')

_calendar = pdt.Calendar(version=pdt.VERSION_CONTEXT_STYLE)


class HumanTime(_TimeBase):
    __slots__ = ()

    def __new__(cls, argument):
        now = datetime.datetime.utcnow()

        dt, status = _calendar.parseDT(argument, sourceTime=now)
        if not status.hasDateOrTime:
            raise commands.BadArgument('Invalid time provided, try e.g. "tomorrow" or "3 days"')

        if not status.hasTime:
            # replace it with the current time
            dt = dt.replace(
                hour=now.hour,
                minute=now.minute,
                second=now.second,
                microsecond=now.microsecond
            )

        return super().__new__(cls, dt)


class Time(HumanTime):
    __slots__ = ()

    def __new__(cls, arg):
        try:
            delta = Delta(arg)
        except commands.BadArgument:
            return super().__new__(cls, arg)
        else:
            now = datetime.datetime.utcnow()
            return _TimeBase.__new__(cls, now + delta.delta)


class FutureTime(Time):
    __slots__ = ()

    def __new__(cls, argument):
        now = datetime.datetime.utcnow()
        self = super().__new__(cls, argument)

        if self.dt < now:
            raise commands.BadArgument('This time is in the past.')

        return self


# TODO: User-friendly Time?

# ------------------------- Parsing -------------------------

TIME_UNITS = ('week', 'day', 'hour', 'minute')


def duration_units(secs):
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    w, d = divmod(d, 7)
    # Weeks, days, hours, and minutes are guaranteed to be integral due to being
    # the quotient rather than the remainder, so these can be safely made to ints.
    # The reason for the int cast is because if the seconds is a float,
    # the other units will be floats too.
    unit_list = [*zip(TIME_UNITS, map(int, (w, d, h, m))),
                 ('second', round(s, 2) if s % 1 else int(s))]
    joined = ', '.join(pluralize(**{u: n}) for u, n in unit_list if n)
    return joined


def parse_delta(delta, *, suffix=''):
    if delta.microseconds and delta.seconds:
        delta = delta + relativedelta(seconds=+1)

    attrs = ['year', 'month', 'day', 'hour', 'minute', 'second']
    elems = (getattr(delta, attr + 's') for attr in attrs)
    output = [pluralize(**{attr: elem}) for attr, elem in zip(attrs, elems) if elem]

    if not output:
        return 'now'
    return human_join(output) + suffix


def human_timedelta(dt, *, source=None):
    now = source or datetime.datetime.utcnow()

    if dt > now:
        delta = relativedelta(dt, now)
        suffix = ''
    else:
        delta = relativedelta(now, dt)
        suffix = ' ago'

    return parse_delta(delta, suffix=suffix)

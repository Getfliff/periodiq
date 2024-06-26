import argparse
import importlib
import logging
import pdb
import sys
from copy import deepcopy
from calendar import monthrange
from datetime import timedelta
from pkg_resources import get_distribution
from queue import Queue
try:
    from signal import (
        SIGALRM,
        alarm,
        signal,
    )
except ImportError:
    SIGALARM = alarm = signal = None
from time import sleep

import pendulum
from dramatiq import Middleware
from dramatiq.cli import (
    LOGFORMAT,
    VERBOSITY,
    import_broker,
)
from dramatiq.middleware import SkipMessage


logger = logging.getLogger('periodiq')


def cron(spec):
    return CronSpec.parse(spec)


class CronSpec:
    _named_spec = {
        '@yearly': "0 0 1 1 *",
        '@annually': "0 0 1 1 *",
        '@monthly': "0 0 1 * *",
        '@weekly': "0 0 * * 0",
        '@daily': "0 0 * * *",
        '@midnight': "0 0 * * *",
        '@hourly': "0 * * * *",
    }

    @classmethod
    def parse(cls, spec):
        # Instanciate a CronSpec object from cron-like string.

        fields = spec.strip()
        if fields.startswith('@'):
            fields = cls._named_spec[fields]
        fields = fields.split()

        # Replace day of week by their number.
        dow = fields[4].lower()
        weekdays = ('sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat')
        for i, day in enumerate(weekdays):
            dow = dow.replace(day, str(i))

        return cls(
            m=expand_valid(fields[0], min=0, max=59),
            h=expand_valid(fields[1], min=0, max=23),
            dom=expand_valid(fields[2], min=1, max=31),
            month=expand_valid(fields[3], min=1, max=12),
            dow=expand_valid(dow, min=0, max=7),
            parsed_from=spec,
        )

    def __init__(self, m, h, dom, month, dow, parsed_from=None):
        self.setup(m, h, dom, month, dow)
        self.parsed_from = parsed_from

    def __eq__(self, other):
        return self.astuple() == other.astuple()

    def __str__(self):
        if self.parsed_from is not None:
            return self.parsed_from
        else:
            return ' '.join([
                format_cron(self.minute, min_=0, max_=59),
                format_cron(self.hour, min_=0, max_=23),
                format_cron(self.dom, min_=1, max_=31),
                format_cron(self.month, min_=1, max_=12),
                format_cron(self.dow, min_=0, max_=7, names=[
                    'Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun',
                ]),
            ])

    def __repr__(self):
        return '<%s %s>' % (self.__class__.__name__, self)

    def astuple(self):
        return self.minute, self.hour, self.dom, self.month, self.dow

    def next_valid_date(self, last):
        # Note about DST. periodiq uses pendulum to have timezone-aware, always
        # valid date. For example, 2019-03-31T02:*:* does not exists in
        # timezone Europe/Paris. Using pendulum .add() methods never produces
        # invalid date. Also, pendulum.now() is always at right timezone.

        # Reset second and microsecond. It's irrelevant for scheduling.
        n = last.replace(second=0, microsecond=0)

        # Next date is at least in one minute.
        n = n.add(minutes=1)

        # How much minutes to way until next valid minute?
        delay_m = first(lambda x: x >= n.minute, self.minute_e) - n.minute
        n = n.add(minutes=delay_m)

        # How much hours to wait until next valid hour?
        delay_h = first(lambda x: x >= n.hour, self.hour_e) - n.hour
        n = n.add(hours=delay_h)

        # How much days to wait until next valid weekday?
        last_dow = n.isoweekday() % 7
        delay_dow = first(lambda x: x >= last_dow, self.dow_e) - last_dow

        # How much days to wait until next valid monthday?
        _, month_days = monthrange(n.year, n.month)
        # Drop irrelevant day of month (28+ or 31+) and adapt offset according
        # to current month.
        dom_e = [
            x for x in self.dom if x <= month_days
        ] + [month_days + self.dom[0]]
        delay_dom = first(lambda x: x >= n.day, dom_e) - n.day

        if self.is_dow_restricted and self.is_dom_restricted:
            # Choose closest day matching dom or dow criteria
            delay_d = min(delay_dow, delay_dom)
        else:
            delay_d = delay_dow if self.is_dow_restricted else delay_dom

        n = n.add(days=delay_d)

        # How much days to wait until next valid month?
        next_month = first(lambda x: x >= n.month, self.month_e)
        delay_d = sum(monthesrange(n.year, n.month, next_month))
        n = n.add(days=delay_d)

        return n

    def replace(self, m=None, h=None, dom=None, month=None, dow=None):
        copy = deepcopy(self)
        copy.setup(m, h, dom, month, dow)
        return copy

    def setup(self, m, h, dom, month, dow):
        # For each field, we compute the next valid value out of bound. i.e if
        # valids minutes are [25, 50], appending 75 will ensure there is always
        # a next valid value for any minute between 0 and 59. timedelta will
        # ensure out of bound minutes are translated to next hour.
        if m is not None:
            self.minute = m
            self.minute_e = m + [60 + m[0]]
        if h is not None:
            self.hour = h
            self.hour_e = h + [24 + h[0]]
        if dom is not None:
            self.is_dom_restricted = len(dom) < 31
            self.dom = dom
        if month is not None:
            self.month = month
            self.month_e = month + [12 + month[0]]
        if dow is not None:
            self.is_dow_restricted = len(dow) < 7
            self.dow = dow
            self.dow_e = dow + [7 + dow[0]]
        # Invalidate string representation.
        self.parsed_from = None

    def validate(self, date):
        # Returns whether this date match the specified constraints.

        if date.minute not in self.minute:
            return False

        if date.hour not in self.hour:
            return False

        if date.month not in self.month:
            return False

        weekday = date.isoweekday()
        if self.is_dow_restricted and self.is_dom_restricted:
            if not (date.day in self.dom or weekday in self.dow):
                return False
        else:
            if date.day not in self.dom:
                return False
            if weekday not in self.dow:
                return False

        return True


def entrypoint():
    logging.basicConfig(level=logging.INFO, format=LOGFORMAT)

    try:
        parser = make_argument_parser()
        args = parser.parse_args()
        exit(main(args))
    except (pdb.bdb.BdbQuit, KeyboardInterrupt):
        logger.info("Interrupted.")
    except Exception:
        logger.exception('Unhandled error:')
        logger.error(
            "Please file an issue at "
            "https://gitlab.com/bersace/periodiq/issues/new with full log.",
        )
    exit(1)


def expand_valid(value, min, max):
    # From cron-like time or date field, expand all valid values within min-max
    # interval.
    valid = set()
    value = value.replace('*', '{min}-{max}'.format(min=min, max=max))
    intervals = value.split(',')
    for interval in intervals:
        range_, _, step = interval.partition('/')
        step = 1 if '' == step else int(step)
        start, _, end = range_.partition('-')
        start = int(start)
        end = start if '' == end else int(end)
        # Note that step is not a modulo. cf.
        # https://stackoverflow.com/questions/27412483/how-do-cron-steps-work
        valid |= set(range(start, end + 1, step))
    return sorted(valid)


def first(function, iterable):
    # Return the first item from iterable accepted by function.
    try:
        return next(x for x in iterable if function(x))
    except StopIteration:
        raise ValueError("No matching value.")


def format_cron(values, min_, max_, names=None):
    if min_ == values[0] and values[-1] == max_:
        return '*'
    else:
        return ','.join(
            format_interval(*i, names=names)
            for i in group_intervals(values)
        )


def format_interval(start, stop, names=None):
    if stop == start:
        return str(start if names is None else names[start])
    elif names:
        return ','.join(names[start:stop+1])
    else:
        return '%s-%s' % (start, stop)


def group_intervals(values):
    last = values[0]
    start = last
    for v in values[1:]:
        diff = v - last
        if diff > 1:
            yield start, last
            start = v
        last = v
    yield start, last


def monthesrange(start_year, start_month, end_month):
    # Switch to zero-base month numbering.
    start_month -= 1
    end_month -= 1
    return (
        x for _, x in (
            monthrange(start_year + m // 12, 1 + m % 12)
            for m in range(start_month, end_month)
        )
    )


def main(args):
    logging.getLogger().setLevel(VERBOSITY.get(args.verbose, logging.DEBUG))
    if alarm is None:
        logger.critical("Unsupported system: alarm syscall is not available.")
        return 1

    logger.info("Starting Periodiq, a simple scheduler for Dramatiq.")

    for path in args.path:
        sys.path.insert(0, path)
    _, broker = import_broker(args.broker)
    for module in args.modules:
        importlib.import_module(module)

    periodic_actors = [
        a for a in broker.actors.values()
        if 'periodic' in a.options
    ]
    if not periodic_actors:
        logger.error("No periodic actor to schedule.")
        return 1
    print_periodic_actors(periodic_actors)

    scheduler = Scheduler(actors=periodic_actors)
    now = pendulum.now()
    # If we start late in a minute. Pad to start of next minute.
    if now.second > 55:
        logger.debug("Skipping to next minute.")
        sleep(60 - now.second)
    scheduler.schedule()
    scheduler.loop()

    return 0


def make_argument_parser():
    dist = get_distribution('periodiq')
    parser = argparse.ArgumentParser(
        prog="periodiq",
        description="Run periodiq scheduler.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "broker",
        help="the broker to use (eg: 'module' or 'module:a_broker')",
    )

    parser.add_argument(
        "modules", metavar="module", nargs="*",
        help="additional python modules to import",
    )

    parser.add_argument(
        "--path", "-P", default=".", nargs="*", type=str,
        help="the module import path (default: %(default)s)",
    )

    parser.add_argument("--version", action="version", version=dist.version)
    parser.add_argument(
        "--verbose", "-v", default=0, action="count",
        help="turn on verbose log output",
    )

    return parser


def print_periodic_actors(actors):
    p = logger.info
    p("Registered periodic actors:")
    p("")
    p("    %-24s module:actor@queue" % ('m h dom mon dow',))
    p("    %-24s ------------------" % ('-' * 24, ))
    for actor in actors:
        kw = dict(
            module=actor.fn.__module__,
            name=actor.actor_name,
            queue=actor.queue_name,
            spec=str(actor.options['periodic']),
        )
        p("    %(spec)-24s %(module)s:%(name)s@%(queue)s " % kw)
    p("")


class PeriodiqMiddleware(Middleware):
    actor_options = set(['periodic'])

    def __init__(self, skip_delay=30):
        self.skip_delay = skip_delay

    def before_process_message(self, broker, message):
        actor = broker.actors[message.actor_name]
        if 'periodic' not in actor.options:
            return

        msg_str = '%s:%s' % (message.message_id, message)
        if 'scheduled_at' not in message.options:
            logger.debug("%s looks manually triggered.", msg_str)
            return

        now = pendulum.now()
        scheduled_at = pendulum.parse(message.options['scheduled_at'])
        delta = now - scheduled_at

        if delta.total_seconds() > self.skip_delay:
            logger.info("Skipping %s older than %ss", msg_str, self.skip_delay)
            raise SkipMessage()
        else:
            logger.info(
                "Processing %s scheduled at %s.",
                msg_str, message.options['scheduled_at'])


class Scheduler:
    def __init__(self, actors):
        self.actors = actors
        # Q for communicating between main process and signal handler.
        self.alarm_q = Queue()

    def loop(self):
        # Block until signal handler sends True.
        while self.alarm_q.get(block=True):
            self.schedule()

    def send_actors(self, actors, now):
        now_str = str(now)
        for actor in actors:
            logger.info("Scheduling %s at %s.", actor, now_str)
            actor.send_with_options(scheduled_at=now_str)

    def schedule(self):
        now = (pendulum.now() + timedelta(seconds=0.5)).replace(microsecond=0)
        logger.debug("Wake up at %s.", now)
        self.send_actors([
            a for a in self.actors
            if a.options['periodic'].validate(now)
        ], now=now)

        prioritized_actors = sorted([
            (actor.options['periodic'].next_valid_date(now), actor)
            for actor in self.actors
        ], key=lambda x: x[0])  # Sort only on date.

        next_date, _ = prioritized_actors[0]
        logger.debug("Nothing to do until %s.", next_date)
        # Refresh now because we may have spent some time sending messages.
        delay = next_date - pendulum.now()
        if delay.total_seconds() <= 0:
            logger.warning("Negative delay. Scheduling immediately.")
            return self.schedule()

        delay_s = delay.total_seconds()
        delay_s, delay_ms = int(delay_s), delay_s % 1
        logger.debug("Sleeping for %ss (%s).", delay_s, delay)
        # Sleep microseconds because alarm only accepts integers.
        sleep(delay_ms)
        if delay_s:
            alarm(delay_s)
            signal(SIGALRM, self.signal_handler)
        else:
            self.alarm_q.put_nowait(True)

    def signal_handler(self, *_):
        logger.debug("Alaaaaarm!")
        self.alarm_q.put_nowait(True)


if '__main__' == __name__:
    entrypoint()

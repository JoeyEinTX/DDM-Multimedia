# la_quiniela/racetime.py - The race's clock: the post time in the race's own time zone
#
# The post time is stored as an absolute time (unix seconds) and entered and
# shown in the race's time zone, LQ_RACE_TZ ("America/Chicago" unless config
# says otherwise): the admin page sends a date and a time as they read on a
# Central clock, the model says "5:57 PM CDT", the TV counts down to the
# instant.
#
# zoneinfo needs a time zone database. DevPi has the system's; a Windows
# machine without the tzdata package has none, so the US zones' rules are
# written out here for that case (since 2007: daylight time from 2:00 on the
# second Sunday in March to 2:00 on the first Sunday in November) and any
# other zone falls back to UTC, with a warning, rather than failing.

import logging
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("la_quiniela.racetime")

DEFAULT_TZ = "America/Chicago"

# name -> (standard offset in hours, standard abbreviation, daylight abbreviation; None: no daylight time)
_US_ZONES: Dict[str, Tuple[int, str, Optional[str]]] = {
    "America/New_York": (-5, "EST", "EDT"),
    "America/Chicago": (-6, "CST", "CDT"),
    "America/Denver": (-7, "MST", "MDT"),
    "America/Phoenix": (-7, "MST", None),
    "America/Los_Angeles": (-8, "PST", "PDT"),
    "US/Eastern": (-5, "EST", "EDT"),
    "US/Central": (-6, "CST", "CDT"),
    "US/Mountain": (-7, "MST", "MDT"),
    "US/Pacific": (-8, "PST", "PDT"),
}

_zones: Dict[str, tzinfo] = {}
_warned: set = set()


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


class _USZone(tzinfo):
    """A US zone by the post-2007 rules, for a machine with no zoneinfo data."""

    def __init__(self, name: str, hours: int, std: str, dst: Optional[str]) -> None:
        self._name = name
        self._std = timedelta(hours=hours)
        self._std_name = std
        self._dst_name = dst

    def __repr__(self) -> str:
        return f"_USZone({self._name!r})"

    def _bounds(self, year: int) -> Tuple[datetime, datetime]:
        """Daylight time's start and end that year as naive local wall times:
        2:00 standard on the second Sunday in March, 2:00 daylight on the
        first Sunday in November (the hour from 1:00 then happens twice)."""
        start = datetime.combine(_nth_sunday(year, 3, 2), time(2))
        end = datetime.combine(_nth_sunday(year, 11, 1), time(1))
        return start, end

    def dst(self, dt: Optional[datetime]) -> timedelta:
        if dt is None or self._dst_name is None:
            return timedelta(0)
        start, end = self._bounds(dt.year)
        wall = dt.replace(tzinfo=None, fold=0)
        if start <= wall < end:
            return timedelta(hours=1)
        if end <= wall < end + timedelta(hours=1):
            return timedelta(hours=1) if dt.fold == 0 else timedelta(0)     # the repeated hour
        return timedelta(0)

    def utcoffset(self, dt: Optional[datetime]) -> timedelta:
        return self._std + self.dst(dt)

    def tzname(self, dt: Optional[datetime]) -> str:
        return self._dst_name if self.dst(dt) else self._std_name

    def fromutc(self, dt: datetime) -> datetime:
        utc = dt.replace(tzinfo=None)
        if self._dst_name is None:
            return (utc + self._std).replace(tzinfo=self)
        start, end = self._bounds((utc + self._std).year)
        start_utc = start - self._std                           # 2:00 standard
        end_utc = end - self._std                               # 2:00 daylight, 1:00 standard
        if start_utc <= utc < end_utc:
            return (utc + self._std + timedelta(hours=1)).replace(tzinfo=self)
        local = (utc + self._std).replace(tzinfo=self)
        if end_utc <= utc < end_utc + timedelta(hours=1):
            local = local.replace(fold=1)                       # the second 1:xx
        return local


def zone(name: Optional[str] = None) -> tzinfo:
    """The zone called `name` (DEFAULT_TZ when empty): zoneinfo's, else the
    written-out US rules, else UTC with one warning."""
    key = (name or DEFAULT_TZ).strip() or DEFAULT_TZ
    found = _zones.get(key)
    if found is not None:
        return found
    try:
        from zoneinfo import ZoneInfo
        found = ZoneInfo(key)
    except Exception:
        spec = _US_ZONES.get(key)
        if spec is not None:
            found = _USZone(key, *spec)
        else:
            if key not in _warned:
                _warned.add(key)
                log.warning("La Quiniela: time zone %r is unknown here (no time zone database); "
                            "race times are shown in UTC", key)
            found = timezone.utc
    _zones[key] = found
    return found


def local_to_epoch(day: Any, hhmm: Any, tz_name: Optional[str] = None) -> float:
    """"2027-05-01" and "17:57" on the race's clock -> unix time. ValueError
    with a message a person can act on for anything else."""
    if not isinstance(day, str) or not isinstance(hhmm, str):
        raise ValueError("date and time must be strings: \"YYYY-MM-DD\" and \"HH:MM\"")
    try:
        d = date.fromisoformat(day.strip())
    except ValueError:
        raise ValueError(f"date {day!r} is not YYYY-MM-DD") from None
    text = hhmm.strip()
    try:
        parts = [int(p, 10) for p in text.split(":")]
        if len(parts) not in (2, 3):
            raise ValueError
        t = time(*parts)
    except ValueError:
        raise ValueError(f"time {hhmm!r} is not HH:MM (24-hour)") from None
    return datetime.combine(d, t).replace(tzinfo=zone(tz_name)).timestamp()


def describe(epoch: float, tz_name: Optional[str] = None) -> Dict[str, Any]:
    """A unix time on the race's clock: {"date": "2027-05-01", "time":
    "17:57", "label": "5:57 PM CDT", "year": 2027, "iso":
    "2027-05-01T17:57:00-05:00"}."""
    local = datetime.fromtimestamp(float(epoch), tz=zone(tz_name))
    hour = local.hour % 12 or 12
    label = f"{hour}:{local.minute:02d} {'PM' if local.hour >= 12 else 'AM'} {local.tzname() or ''}".strip()
    return {
        "date": local.date().isoformat(),
        "time": f"{local.hour:02d}:{local.minute:02d}",
        "label": label,
        "year": local.year,
        "iso": local.isoformat(timespec="seconds"),
    }

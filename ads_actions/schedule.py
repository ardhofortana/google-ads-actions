"""Ad schedule arithmetic: validation and shifting slots across midnight.

Slots are whole hours in the account time zone. Internally a slot becomes a
half-open interval of week hours, Monday 00:00 = 0, Sunday 24:00 = 168.
"""

from dataclasses import dataclass

from ads_actions.errors import Refused

DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
WEEK = 168
MAX_SLOTS_PER_DAY = 6  # Google Ads limit


@dataclass(frozen=True, order=True)
class Slot:
    day: str
    start_hour: int
    end_hour: int
    bid_modifier: float | None = None

    def sort_key(self) -> tuple[int, int]:
        return DAYS.index(self.day), self.start_hour

    def to_dict(self) -> dict:
        d = {"day": self.day, "start_hour": self.start_hour, "end_hour": self.end_hour}
        if self.bid_modifier is not None:
            d["bid_modifier"] = self.bid_modifier
        return d

    def label(self) -> str:
        mod = f" (bid x{self.bid_modifier:g})" if self.bid_modifier is not None else ""
        return f"{self.day.title()[:3]} {self.start_hour:02d}:00-{self.end_hour:02d}:00{mod}"


def parse_slots(raw: list[dict]) -> list[Slot]:
    """Validates caller-supplied slots and returns them sorted."""
    slots = []
    for item in raw:
        day = str(item.get("day", "")).strip().upper()
        if day not in DAYS:
            # Accept 3-letter abbreviations.
            matches = [d for d in DAYS if d.startswith(day[:3])] if len(day) >= 3 else []
            if len(matches) != 1:
                raise Refused(f"Unknown day {item.get('day')!r}.")
            day = matches[0]
        try:
            start, end = item["start_hour"], item["end_hour"]
        except KeyError:
            raise Refused("Every slot needs day, start_hour and end_hour.")
        if not (isinstance(start, int) and isinstance(end, int)) or isinstance(start, bool) or isinstance(end, bool):
            raise Refused("Hours must be whole numbers.")
        if not (0 <= start < end <= 24):
            raise Refused(f"Slot {day} {start}-{end} is invalid: need 0 <= start < end <= 24.")
        mod = item.get("bid_modifier")
        slots.append(Slot(day, start, end, float(mod) if mod is not None else None))
    slots.sort(key=Slot.sort_key)
    check_no_overlap(slots)
    return slots


def check_no_overlap(slots: list[Slot]) -> None:
    per_day: dict[str, list[Slot]] = {}
    for s in slots:
        per_day.setdefault(s.day, []).append(s)
    for day, ds in per_day.items():
        ds.sort(key=Slot.sort_key)
        for a, b in zip(ds, ds[1:]):
            if b.start_hour < a.end_hour:
                raise Refused(f"Slots overlap on {day}: {a.label()} and {b.label()}.")
        if len(ds) > MAX_SLOTS_PER_DAY:
            raise Refused(f"{day} has {len(ds)} slots; Google Ads allows {MAX_SLOTS_PER_DAY} per day.")


def shift_slots(slots: list[Slot], hours: int) -> list[Slot]:
    """Shifts every slot by ``hours``, wrapping across midnight and the week.

    Pieces that touch after shifting are merged (when their bid modifiers match)
    and then split at day boundaries, so an hour that spills past midnight joins
    the next day's early slot instead of becoming a separate one.
    """
    # 1. Shift to week-hour intervals, splitting at the week boundary.
    pieces: list[tuple[int, int, float | None]] = []
    for s in slots:
        a = DAYS.index(s.day) * 24 + s.start_hour + hours
        b = DAYS.index(s.day) * 24 + s.end_hour + hours
        a_mod, b_mod = a % WEEK, b % WEEK
        if b_mod == 0:
            b_mod = WEEK
        if a_mod < b_mod:
            pieces.append((a_mod, b_mod, s.bid_modifier))
        else:
            pieces.append((a_mod, WEEK, s.bid_modifier))
            pieces.append((0, b_mod, s.bid_modifier))
    pieces.sort(key=lambda p: (p[0], p[1]))

    # 2. Merge touching or overlapping pieces with the same modifier.
    merged: list[list] = []
    for a, b, m in pieces:
        if merged and a <= merged[-1][1] and merged[-1][2] == m:
            merged[-1][1] = max(merged[-1][1], b)
        elif merged and a < merged[-1][1]:
            raise Refused("Shifting would overlap slots that have different bid modifiers.")
        else:
            merged.append([a, b, m])

    # 3. Split back into per-day slots.
    out: list[Slot] = []
    for a, b, m in merged:
        while a < b:
            day_idx = a // 24
            day_end = (day_idx + 1) * 24
            end = min(b, day_end)
            out.append(Slot(DAYS[day_idx], a - day_idx * 24, end - day_idx * 24, m))
            a = end
    out.sort(key=Slot.sort_key)
    check_no_overlap(out)
    return out

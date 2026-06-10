"""Turn a column into believable values.

Three layers, each falling back to the one below so the tool runs with nothing
installed:

1. Column-name heuristics. ``email`` looks like an email, ``price`` is money,
   ``created_at`` is a recent timestamp. This is what makes the output read
   like real data instead of ``text_1, text_2``.
2. Optional Faker for richer names/addresses when it's installed.
3. Optional NumPy for skewed numeric distributions (lognormal money, zipf
   counts) generated in a vectorized batch. Without NumPy you get a uniform
   stdlib draw.

The public entry point is `ValueFactory`, which a caller asks for one value at
a time, or for a whole column batch (so NumPy can vectorize).
"""

from __future__ import annotations

import datetime as dt
import random
import string
from dataclasses import replace
from decimal import Decimal
from typing import Any, Optional

from .model import Column


class FeasibilityError(ValueError):
    """A UNIQUE column cannot supply the requested number of distinct values.

    Carries the column, the requested count, and either the computed domain
    `capacity` (the domain is provably too small) or how many distinct values
    were `achieved` before the domain was exhausted — so the message tells you
    *why* it failed, not just that it did.
    """

    def __init__(self, column: "Column", n: int,
                 capacity: int | None = None, achieved: int | None = None) -> None:
        self.column = column
        self.n = n
        self.capacity = capacity
        self.achieved = achieved
        if capacity is not None:
            msg = (
                f"{column.name}: UNIQUE {column.type} column can supply at most "
                f"~{capacity} distinct values, but {n} rows were requested"
            )
        else:
            msg = (
                f"{column.name}: generated only {achieved} distinct values for "
                f"{n} requested rows ({column.type} domain exhausted)"
            )
        super().__init__(msg)

try:  # optional, vectorized numeric distributions
    import numpy as _np
except ImportError:  # pragma: no cover - exercised by env without numpy
    _np = None

try:  # optional, richer realistic strings
    from faker import Faker as _Faker
except ImportError:
    _Faker = None


_FIRST_NAMES = (
    "James Mary John Patricia Robert Jennifer Michael Linda David Elizabeth "
    "Maria Ravi Wei Chen Aisha Omar Sofia Liam Noah Olivia Ava Lucas"
).split()
_LAST_NAMES = (
    "Smith Johnson Williams Brown Jones Garcia Miller Davis Rodriguez Martinez "
    "Patel Nguyen Kim Khan Singh Lopez Cohen Ali Murphy Reed"
).split()
_STATUSES = ["ACTIVE", "PENDING", "CLOSED", "SUSPENDED", "CANCELLED"]
_CITIES = [
    "Austin", "Boston", "Chicago", "Denver", "Miami", "Phoenix", "Portland",
    "Raleigh", "Seattle", "Tampa",
]
_PERSON_NAME_HINTS = (
    "full", "first", "last", "contact", "customer", "user",
    "employee", "person", "requester", "agent", "owner",
)
_LOREM_WORDS = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua ut enim ad minim "
    "veniam quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea "
    "commodo consequat duis aute irure dolor in reprehenderit in voluptate "
    "velit esse cillum dolore eu fugiat nulla pariatur"
).split()


class ValueFactory:
    def __init__(self, seed: Optional[int] = None) -> None:
        self.rng = random.Random(seed)
        self._np_rng = _np.random.default_rng(seed) if _np is not None else None
        self._faker = None
        if _Faker is not None:
            self._faker = _Faker()
            if seed is not None:
                self._faker.seed_instance(seed)
    # -- batch API: lets NumPy fill a numeric column in one vectorized call ----
    def batch(self, column: Column, n: int) -> list[Any]:
        if column.numeric_precision is not None or column.numeric_scale is not None:
            return [self.one(column, i) for i in range(n)]
        if self._np_rng is not None and column.type in ("integer", "real", "numeric"):
            return self._numeric_batch(column, n)
        return [self.one(column, i) for i in range(n)]

    def _numeric_batch(self, column: Column, n: int) -> list[Any]:
        name = column.name.lower()
        if column.type == "integer":
            if any(k in name for k in ("qty", "quantity", "count", "num")):
                # zipf-ish skew: most small, a few large
                vals = self._np_rng.integers(1, 50, size=n)
            elif "age" in name:
                vals = self._np_rng.integers(18, 90, size=n)
            else:
                vals = self._np_rng.integers(0, 10_000, size=n)
            return [int(v) for v in vals]
        # real / numeric -> money-like lognormal when the name smells like money
        if any(k in name for k in ("price", "amount", "cost", "total", "balance", "fee")):
            vals = self._np_rng.lognormal(mean=3.0, sigma=1.0, size=n)
        else:
            vals = self._np_rng.uniform(0, 1000, size=n)
        return [round(float(v), 2) for v in vals]

    # -- single value ----------------------------------------------------------
    def one(self, column: Column, row_index: int) -> Any:
        if column.nullable and not column.primary_key and self.rng.random() < 0.07:
            return None

        name = column.name.lower()
        t = column.type

        if t == "boolean":
            return self.rng.choice([False, True])
        if t in ("date", "datetime"):
            return self._temporal(name, t)
        if t == "blob":
            return self._blob()
        if t in ("integer", "real", "numeric"):
            # reachable when NumPy is absent
            return self._numeric_scalar(column, name, t)

        # text from here down
        return self._text(column, name, row_index)

    def _numeric_scalar(self, column: Column, name: str, t: str) -> Any:
        constrained = self._constrained_numeric(column)
        if constrained is not None:
            return constrained

        if t == "integer":
            if any(k in name for k in ("qty", "quantity", "count", "num")):
                return self.rng.randint(1, 50)
            if "age" in name:
                return self.rng.randint(18, 90)
            return self.rng.randint(0, 10_000)
        if any(k in name for k in ("price", "amount", "cost", "total", "balance", "fee")):
            return round(self.rng.uniform(1, 1000), 2)
        return round(self.rng.uniform(0, 1000), 2)

    def _constrained_numeric(self, column: Column) -> Any | None:
        precision = column.numeric_precision
        scale = column.numeric_scale
        if precision is None and scale is None:
            return None

        scale = scale or 0
        if scale <= 0:
            max_value = self._max_integer_for_precision(precision)
            return self.rng.randint(0, max_value)

        whole_digits = max((precision or 6) - scale, 0)
        max_units = (10 ** whole_digits) * (10 ** scale) - 1
        units = self.rng.randint(0, max_units)
        return Decimal(units).scaleb(-scale).quantize(Decimal(1).scaleb(-scale))

    @staticmethod
    def _max_integer_for_precision(precision: int | None) -> int:
        if precision is None:
            return 10_000
        return max(0, 10 ** precision - 1)

    def _temporal(self, name: str, t: str):
        # Return real date/datetime objects, not strings. A SQLite literal and an
        # Oracle TO_DATE(...) literal are spelled completely differently, so the
        # value must stay a typed object until a *dialect* renders it. Baking in
        # isoformat() here would have hard-coded one engine's idea of a date.
        base = dt.date(2023, 1, 1)
        day = base + dt.timedelta(days=self.rng.randint(0, 900))
        if t == "date":
            return day
        return dt.datetime(day.year, day.month, day.day) + dt.timedelta(
            seconds=self.rng.randint(0, 86399)
        )

    def _blob(self) -> bytes:
        return bytes(self.rng.getrandbits(8) for _ in range(16))

    def _text(self, column: Column, name: str, row_index: int) -> str:
        max_len = column.max_length
        if "email" in name:
            value = self._email(row_index, max_len)
        elif "first" in name and "name" in name:
            value = self._faker.first_name() if self._faker else self.rng.choice(_FIRST_NAMES)
        elif "last" in name and "name" in name:
            value = self._faker.last_name() if self._faker else self.rng.choice(_LAST_NAMES)
        elif name.endswith("name") or name == "name":
            value = self._a_name(name)
        elif "phone" in name:
            value = self._phone(max_len)
        elif "status" in name:
            value = self._choice_that_fits(_STATUSES, max_len) or self._token(max_len or 6)
        elif "city" in name:
            value = self._faker.city() if self._faker else self.rng.choice(_CITIES)
        elif "code" in name:
            value = self._token(max_len or 8)
        elif self._faker:
            value = self._faker.sentence(nb_words=4).rstrip(".")
        else:
            value = self._words_that_fit(max_len)

        return self._fit_text(value, max_len)

    def _a_name(self, name: str) -> str:
        """A person name for person-ish columns, an entity label otherwise."""
        person = name == "name" or any(h in name for h in _PERSON_NAME_HINTS)
        if person:
            if self._faker:
                return self._faker.name()
            return f"{self.rng.choice(_FIRST_NAMES)} {self.rng.choice(_LAST_NAMES)}"
        # group_name, category_name, ci_name, product_name, ...
        words = self.rng.choices(_LOREM_WORDS, k=self.rng.randint(1, 2))
        return " ".join(w.title() for w in words)

    def _email(self, row_index: int, max_len: int | None) -> str:
        domain = "example.com"
        if max_len is None:
            base = self._faker.user_name() if self._faker else "user"
            return f"{base}{row_index}@{domain}"

        shortest_domain = "x.io"
        suffix = f"@{domain}"
        local_room = max_len - len(suffix)
        if local_room >= 1:
            local = self._token(local_room)
            return f"{local}{suffix}"

        suffix = f"@{shortest_domain}"
        local_room = max_len - len(suffix)
        if local_room >= 1:
            local = self._token(local_room)
            return f"{local}{suffix}"

        return self._token(max_len)

    def _phone(self, max_len: int | None) -> str:
        digits = "".join(self.rng.choices(string.digits, k=10))
        if max_len is not None and max_len < 14:
            return digits[:max_len]
        if self._faker:
            return self._faker.phone_number()
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"

    def _words_that_fit(self, max_len: int | None) -> str:
        if max_len is None:
            return self._lorem_sentence()

        choices = [w for w in _LOREM_WORDS if len(w) <= max_len]
        if not choices:
            return self._token(max_len)

        words: list[str] = []
        while choices and len(words) < 4:
            candidate = self.rng.choice(choices)
            trial = " ".join([*words, candidate])
            if len(trial) > max_len:
                break
            words.append(candidate)
            if self.rng.random() < 0.35:
                break
        return " ".join(words) if words else self._token(max_len)

    def _lorem_sentence(self) -> str:
        size = self.rng.randint(8, 18)
        words = self.rng.choices(_LOREM_WORDS, k=size)
        return " ".join(words).capitalize() + "."

    def _choice_that_fits(self, choices: list[str], max_len: int | None) -> str | None:
        fitting = [v for v in choices if max_len is None or len(v) <= max_len]
        return self.rng.choice(fitting) if fitting else None

    def _token(self, max_len: int) -> str:
        if max_len <= 0:
            return ""
        size = min(max_len, 8)
        return "".join(self.rng.choices(string.ascii_uppercase + string.digits, k=size))

    @staticmethod
    def _fit_text(value: str, max_len: int | None) -> str:
        if max_len is None or len(value) <= max_len:
            return value
        return value[:max_len]

    # -- distinct value allocation for UNIQUE columns --------------------------
    def distinct_values(self, column: Column, n: int) -> list[Any]:
        """Return n distinct, non-null values for a UNIQUE column.

        Sample without replacement where a bounded domain is known (random-
        looking but collision-free), fall back to a sequential range when the
        integer domain is unbounded, and raise FeasibilityError up front when
        the declared domain genuinely cannot supply n distinct values.
        """
        if n <= 0:
            return []
        t = column.type
        if t == "boolean":
            if n > 2:
                raise FeasibilityError(column, n, capacity=2)
            return self.rng.sample([False, True], n)
        if t == "integer":
            return self._distinct_integers(column, n)
        if t == "date":
            return self._distinct_dates(column, n)
        if t == "datetime":
            return self._distinct_datetimes(column, n)
        # numeric / real / text / blob: generate and de-duplicate, bounded
        return self._distinct_by_generation(column, n)

    def _distinct_integers(self, column: Column, n: int) -> list[int]:
        lo, hi = self._integer_range(column)
        capacity = hi - lo + 1
        if capacity < n:
            if column.numeric_precision is not None:
                # a declared precision is a hard domain limit -> fail fast
                raise FeasibilityError(column, n, capacity=capacity)
            hi = lo + n - 1  # unbounded integer: widen the window to fit
        try:
            return self.rng.sample(range(lo, hi + 1), n)   # sample: random-looking
        except (ValueError, OverflowError):                # pragma: no cover
            return [lo + i for i in range(n)]              # sequential fallback

    def _integer_range(self, column: Column) -> tuple[int, int]:
        if column.numeric_precision is not None and (column.numeric_scale or 0) == 0:
            return 0, 10 ** column.numeric_precision - 1
        name = column.name.lower()
        if any(k in name for k in ("qty", "quantity", "count", "num")):
            return 1, 50
        if "age" in name:
            return 18, 90
        return 0, 10_000

    def _distinct_dates(self, column: Column, n: int) -> list[dt.date]:
        window = 901  # matches the generator's date window, in days
        if n > window:
            raise FeasibilityError(column, n, capacity=window)
        base = dt.date(2023, 1, 1)
        return [base + dt.timedelta(days=o) for o in self.rng.sample(range(window), n)]

    def _distinct_datetimes(self, column: Column, n: int) -> list[dt.datetime]:
        window = 901 * 86400
        if n > window:
            raise FeasibilityError(column, n, capacity=window)
        base = dt.datetime(2023, 1, 1)
        return [base + dt.timedelta(seconds=o)
                for o in self.rng.sample(range(window), n)]

    def _distinct_by_generation(self, column: Column, n: int) -> list[Any]:
        non_null = replace(column, nullable=False)  # never put NULL in a unique set
        seen: set[Any] = set()
        out: list[Any] = []
        misses = 0
        miss_budget = max(1000, 5 * n)  # consecutive misses before declaring exhaustion
        while len(out) < n:
            value = self.one(non_null, len(out))
            key = (bytes(value) if isinstance(value, (bytes, bytearray, memoryview))
                   else value)
            if key in seen:
                misses += 1
                if misses > miss_budget:
                    raise FeasibilityError(column, n, achieved=len(out))
                continue
            seen.add(key)
            out.append(value)
            misses = 0
        return out

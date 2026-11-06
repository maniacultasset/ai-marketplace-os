"""Indian reference values (SI1 T3).

`STATE_CODES`: the 36 ISO 3166-2:IN subdivision codes in use since 2020, 28 states and 8 union
territories, without the `IN-` prefix. `DH` is Dadra and Nagar Haveli and Daman and Diu (merged
2020), `LA` is Ladakh, `TG` is Telangana and `OD` is Odisha (the old `OR` is gone).
"""

from __future__ import annotations

from typing import Final

STATES: Final = (
    "AP", "AR", "AS", "BR", "CT", "GA", "GJ", "HR", "HP", "JH", "KA", "KL", "MP", "MH",
    "MN", "ML", "MZ", "NL", "OD", "PB", "RJ", "SK", "TN", "TG", "TR", "UP", "UT", "WB",
)  # fmt: skip
UNION_TERRITORIES: Final = ("AN", "CH", "DH", "DL", "JK", "LA", "LD", "PY")

STATE_CODES: Final[frozenset[str]] = frozenset(STATES + UNION_TERRITORIES)

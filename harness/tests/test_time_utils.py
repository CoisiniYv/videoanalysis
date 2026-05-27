"""F2.4 — timestamp normalization unit tests."""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from modules.savant_security.custom.services.time_utils import normalize_pts_to_ms


class TestNormalizePtsToMs:
    def test_none_returns_zero(self):
        assert normalize_pts_to_ms(None) == 0

    def test_zero_returns_zero(self):
        assert normalize_pts_to_ms(0) == 0

    def test_small_milliseconds_passthrough(self):
        # Values under 10^7 are assumed already ms
        assert normalize_pts_to_ms(1000) == 1000
        assert normalize_pts_to_ms(5000) == 5000

    def test_nanoseconds_converted(self):
        # 1 second = 1_000_000_000 ns → 1000 ms
        assert normalize_pts_to_ms(1_000_000_000) == 1000
        # 43 seconds in ns ≈ 4.3e10 → 43000 ms
        assert normalize_pts_to_ms(43_000_000_000) == 43000

    def test_realistic_savant_pts_ns(self):
        # Savant PTS in nanoseconds: 46229120000000 → 46229120 ms
        pts_ns = 46_229_120_000_000
        expected_ms = 46_229_120
        assert normalize_pts_to_ms(pts_ns) == expected_ms

    def test_microseconds_converted(self):
        # Values in [10^7, 10^9) treated as microseconds
        # 1 second = 10_000_000 µs → 10000 ms
        assert normalize_pts_to_ms(10_000_000) == 10000
        # 43 seconds in µs = 43_000_000 → 43000 ms
        assert normalize_pts_to_ms(43_000_000) == 43000

    def test_returns_int(self):
        result = normalize_pts_to_ms(1_000_000_000)
        assert isinstance(result, int)

    def test_negative_pts_returns_zero(self):
        assert normalize_pts_to_ms(-1000) == 0

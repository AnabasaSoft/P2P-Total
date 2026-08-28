"""core/ip_filter.py: filtro de IPs estilo aMule/eMule (ipfilter.dat)."""

import pytest

from core.ip_filter import IPFilter

SAMPLE = """\
# comentario ignorado
1.2.3.0 - 1.2.3.255 , 50 , Rango peligroso
10.0.0.0 - 10.0.0.255 , 200 , Rango tolerado (nivel alto)
this line does not match the format at all
"""


def test_load_parses_valid_ranges_and_ignores_junk_lines(tmp_path):
    path = tmp_path / "ipfilter.dat"
    path.write_text(SAMPLE, encoding="utf-8")

    f = IPFilter()
    count = f.load(str(path))

    assert count == 2
    assert f.rule_count() == 2


def test_load_missing_file_returns_zero_without_raising(tmp_path):
    f = IPFilter()
    count = f.load(str(tmp_path / "no-existe.dat"))

    assert count == 0
    assert f.rule_count() == 0


def test_is_blocked_true_for_ip_in_range_at_or_below_threshold(tmp_path):
    path = tmp_path / "ipfilter.dat"
    path.write_text(SAMPLE, encoding="utf-8")

    f = IPFilter()
    f.load(str(path))
    f.configure(enabled=True, level_threshold=127)

    assert f.is_blocked("1.2.3.42") is True


def test_is_blocked_false_for_ip_in_range_above_threshold(tmp_path):
    path = tmp_path / "ipfilter.dat"
    path.write_text(SAMPLE, encoding="utf-8")

    f = IPFilter()
    f.load(str(path))
    f.configure(enabled=True, level_threshold=127)

    # 10.0.0.0/24 tiene nivel 200, por encima del umbral 127: no se bloquea.
    assert f.is_blocked("10.0.0.42") is False


def test_is_blocked_false_for_ip_outside_any_range(tmp_path):
    path = tmp_path / "ipfilter.dat"
    path.write_text(SAMPLE, encoding="utf-8")

    f = IPFilter()
    f.load(str(path))
    f.configure(enabled=True, level_threshold=127)

    assert f.is_blocked("8.8.8.8") is False


def test_is_blocked_false_when_disabled(tmp_path):
    path = tmp_path / "ipfilter.dat"
    path.write_text(SAMPLE, encoding="utf-8")

    f = IPFilter()
    f.load(str(path))
    f.configure(enabled=False, level_threshold=127)

    assert f.is_blocked("1.2.3.42") is False


def test_is_blocked_false_for_non_ipv4_host():
    f = IPFilter()
    f.configure(enabled=True, level_threshold=127)

    assert f.is_blocked("example.org") is False


def test_blocked_ranges_returns_dotted_quad_pairs_above_threshold(tmp_path):
    path = tmp_path / "ipfilter.dat"
    path.write_text(SAMPLE, encoding="utf-8")

    f = IPFilter()
    f.load(str(path))
    f.configure(enabled=True, level_threshold=127)

    assert f.blocked_ranges() == [("1.2.3.0", "1.2.3.255")]


def test_blocked_ranges_empty_when_disabled(tmp_path):
    path = tmp_path / "ipfilter.dat"
    path.write_text(SAMPLE, encoding="utf-8")

    f = IPFilter()
    f.load(str(path))
    f.configure(enabled=False, level_threshold=127)

    assert f.blocked_ranges() == []

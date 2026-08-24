"""core/bandwidth_scheduler.py: planificador de ancho de banda por
franja horaria (punto 34.5 del backlog)."""

from datetime import time as dt_time

from core.bandwidth_scheduler import BandwidthScheduler, effective_limits_kbps, is_within_schedule
from core.config import Config


def test_is_within_schedule_same_start_and_end_is_always_active():
    assert is_within_schedule("10:00", "10:00", now=dt_time(3, 0)) is True
    assert is_within_schedule("10:00", "10:00", now=dt_time(23, 59)) is True


def test_is_within_schedule_normal_range_does_not_cross_midnight():
    assert is_within_schedule("09:00", "18:00", now=dt_time(12, 0)) is True
    assert is_within_schedule("09:00", "18:00", now=dt_time(8, 59)) is False
    assert is_within_schedule("09:00", "18:00", now=dt_time(18, 0)) is False  # el fin es exclusivo


def test_is_within_schedule_range_crossing_midnight():
    assert is_within_schedule("23:00", "07:00", now=dt_time(23, 30)) is True
    assert is_within_schedule("23:00", "07:00", now=dt_time(3, 0)) is True
    assert is_within_schedule("23:00", "07:00", now=dt_time(12, 0)) is False


def test_effective_limits_uses_global_limits_when_schedule_disabled():
    config = Config(global_download_limit_kbps=500, global_upload_limit_kbps=100)
    config.schedule.enabled = False
    config.schedule.download_limit_kbps = 50
    config.schedule.upload_limit_kbps = 10
    assert effective_limits_kbps(config, now=dt_time(12, 0)) == (500, 100)


def test_effective_limits_uses_schedule_limits_when_active():
    config = Config(global_download_limit_kbps=500, global_upload_limit_kbps=100)
    config.schedule.enabled = True
    config.schedule.start = "22:00"
    config.schedule.end = "07:00"
    config.schedule.download_limit_kbps = 50
    config.schedule.upload_limit_kbps = 10
    assert effective_limits_kbps(config, now=dt_time(23, 30)) == (50, 10)


def test_effective_limits_falls_back_to_global_outside_schedule_window():
    config = Config(global_download_limit_kbps=500, global_upload_limit_kbps=100)
    config.schedule.enabled = True
    config.schedule.start = "22:00"
    config.schedule.end = "07:00"
    config.schedule.download_limit_kbps = 50
    config.schedule.upload_limit_kbps = 10
    assert effective_limits_kbps(config, now=dt_time(12, 0)) == (500, 100)


def test_bandwidth_scheduler_applies_limits_only_on_transition(monkeypatch):
    calls = []
    scheduler = BandwidthScheduler(lambda: calls.append(1))

    disabled_config = Config()
    disabled_config.schedule.enabled = False

    enabled_config = Config()
    enabled_config.schedule.enabled = True
    enabled_config.schedule.start = "00:00"
    enabled_config.schedule.end = "00:00"  # franja de 24h: siempre activa

    monkeypatch.setattr("core.bandwidth_scheduler.load_config", lambda: disabled_config)
    scheduler._check_once()
    assert len(calls) == 1  # primera evaluación (estado previo desconocido): reaplica para fijar la línea base

    scheduler._check_once()
    assert len(calls) == 1  # sigue desactivado, sin transición: no vuelve a reaplicar

    monkeypatch.setattr("core.bandwidth_scheduler.load_config", lambda: enabled_config)
    scheduler._check_once()
    assert len(calls) == 2  # transición de desactivado a activo: reaplica

    scheduler._check_once()
    assert len(calls) == 2  # sigue activo, sin transición: no vuelve a reaplicar

    monkeypatch.setattr("core.bandwidth_scheduler.load_config", lambda: disabled_config)
    scheduler._check_once()
    assert len(calls) == 3  # transición de vuelta a desactivado: reaplica otra vez

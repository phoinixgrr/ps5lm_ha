"""Tests for the Prometheus text parser."""

from custom_components.ps5_launcher.metrics import first, parse_prometheus

from .conftest import EXPORTER_METRICS


def test_parses_samples_and_labels():
    m = parse_prometheus(EXPORTER_METRICS)
    assert first(m, "ps5_fan_duty_ratio") == 0.1855
    assert first(m, "ps5_temperature_celsius", sensor="soc1") == 47
    assert len(m["ps5_temperature_celsius"]) == 3


def test_label_called_name_does_not_clash():
    m = parse_prometheus(EXPORTER_METRICS)
    assert first(m, "ps5_process_resident_bytes", name="eboot.bin") == 9974677504


def test_skips_comments_and_garbage():
    m = parse_prometheus("# HELP x y\nnot a metric line at all {\nvalid_metric 1\nbad_value abc\n")
    assert first(m, "valid_metric") == 1
    assert "bad_value" not in m


def test_missing_metric_or_label_returns_none():
    m = parse_prometheus(EXPORTER_METRICS)
    assert first(m, "does_not_exist") is None
    assert first(m, "ps5_temperature_celsius", sensor="gpu") is None


def test_colons_in_metric_names():
    m = parse_prometheus("llamacpp:requests_processing 2\n")
    assert first(m, "llamacpp:requests_processing") == 2

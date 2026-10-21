"""Tests for the Prometheus text parser."""

from custom_components.ps5_launcher.metrics import first, model_metrics_to_show, parse_prometheus

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


# -- holding the model readings across a restart -----------------------------------------------------------------------
FRESH = {"model": "Qwen3-14B-Q4_K_M", "context_size": 32768, "tok_per_s": 17.7}


def test_fresh_readings_are_shown_and_remembered():
    shown, remembered = model_metrics_to_show(FRESH, {}, pending=False, switching_to=None)
    assert shown == FRESH
    assert remembered == FRESH


def test_silence_while_nothing_is_running_drops_the_readings():
    """PS5LM is really gone, so those sensors should go unavailable rather than show a stale number."""
    shown, remembered = model_metrics_to_show({}, FRESH, pending=False, switching_to=None)
    assert shown == {}
    assert remembered == {}


def test_silence_during_an_operation_holds_the_readings():
    shown, remembered = model_metrics_to_show({}, FRESH, pending=True, switching_to=None)
    assert shown == FRESH
    assert remembered == FRESH  # still the fallback for the next poll of the same restart


def test_a_switch_names_the_model_being_loaded():
    """The old model is already unloaded, so showing its name for two minutes would be wrong."""
    shown, remembered = model_metrics_to_show({}, FRESH, pending=True, switching_to="Qwen3.5-9B-Q6_K")
    assert shown["model"] == "Qwen3.5-9B-Q6_K"
    assert shown["tok_per_s"] == 17.7  # the other readings are the last ones from before the restart
    assert remembered["model"] == "Qwen3-14B-Q4_K_M"


def test_nothing_to_hold_yet():
    """A switch started before the server was ever read: no invented readings."""
    assert model_metrics_to_show({}, {}, pending=True, switching_to="Qwen3.5-9B-Q6_K") == ({}, {})


def test_holding_does_not_change_the_remembered_readings():
    last = dict(FRESH)
    model_metrics_to_show({}, last, pending=True, switching_to="other")
    assert last == FRESH

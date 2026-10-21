"""Minimal Prometheus text-format parser."""

from __future__ import annotations

import re

_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_prometheus(text: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    """Returns {metric name: [(labels, value), ...]}. Comments and malformed lines are skipped."""
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            continue
        name, raw_labels, raw_value = match.groups()
        try:
            value = float(raw_value)
        except ValueError:
            continue
        labels = {k: v for k, v in _LABEL.findall(raw_labels)} if raw_labels else {}
        out.setdefault(name, []).append((labels, value))
    return out


def first(metrics: dict, metric: str, /, **labels: str) -> float | None:
    """Value of the first sample of `metric` whose labels include all `labels` (a label may itself be called `name`)."""
    for sample_labels, value in metrics.get(metric, []):
        if all(sample_labels.get(k) == v for k, v in labels.items()):
            return value
    return None


def model_metrics_to_show(fresh: dict, last: dict, pending: bool, switching_to: str | None) -> tuple[dict, dict]:
    """The model-server readings to publish now, and the ones to remember for next time.

    Restarting PS5LM on purpose (a model switch, or the Server switch) leaves its server silent for about two minutes.
    The last readings are held for that time, with the model named as the one being loaded, so a dashboard does not
    empty out and refill; Status and Model ready still say what is really happening. Silence while no operation is
    running means the server is genuinely gone, so the readings are dropped and those sensors go unavailable.
    """
    if fresh:
        return fresh, fresh
    if not pending:
        return {}, {}
    held = dict(last)
    if held and switching_to:
        held["model"] = switching_to
    return held, last

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

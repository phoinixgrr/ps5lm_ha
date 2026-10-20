"""Shared fixtures for the PS5LM integration tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.ps5_launcher.ps5client import PS5Client  # noqa: E402

REGIST_KEY = "1a2b3c4d"
MORNING = "00112233445566778899aabbccddeeff"

EXPORTER_METRICS = """\
# HELP ps5_temperature_celsius Temperature reported by the console.
# TYPE ps5_temperature_celsius gauge
ps5_temperature_celsius{sensor="cpu"} 45
ps5_temperature_celsius{sensor="soc0"} 41
ps5_temperature_celsius{sensor="soc1"} 47
ps5_fan_duty_ratio 0.1855
ps5_uptime_seconds 3747
ps5_process_resident_bytes{name="eboot.bin"} 9974677504
ps5_process_resident_bytes{name="SceShellUI"} 815792128
ps5_filesystem_avail_bytes{mountpoint="/user",fstype="bfs"} 208546955264
ps5_filesystem_avail_bytes{mountpoint="/mnt/usb0",fstype="exfatfs"} 199925694464
"""


def llama_metrics(predicted: float, predicted_s: float, prompt: float, prompt_s: float, cached: float = 0) -> str:
    return f"""\
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total {prompt}
llamacpp:prompt_tokens_cached_total {cached}
llamacpp:prompt_seconds_total {prompt_s}
llamacpp:tokens_predicted_total {predicted}
llamacpp:tokens_predicted_seconds_total {predicted_s}
llamacpp:predicted_tokens_seconds 0
llamacpp:requests_processing 1
llamacpp:requests_deferred 0
"""


PROPS = '{"model_path": "/data/PS5LM/models/Qwen3.5-9B-Q6_K.gguf", "default_generation_settings": {"n_ctx": 65536}}'


@pytest.fixture
def client() -> PS5Client:
    return PS5Client("192.0.2.10", REGIST_KEY, MORNING, "PPSA99575", ready_url="http://192.0.2.10:8081/health")

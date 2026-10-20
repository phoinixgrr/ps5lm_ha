"""Constants for the PS5 Launcher integration."""

DOMAIN = "ps5_launcher"

CONF_REGIST_KEY = "regist_key"
CONF_MORNING = "morning"
CONF_TITLE_ID = "title_id"
CONF_WEB_PORT = "web_port"
CONF_READY_URL = "ready_url"
CONF_EXPORTER_PORT = "exporter_port"
CONF_CONSOLE_INTERVAL = "console_interval"
CONF_API_URL = "api_url"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_LLM_METRICS = "llm_metrics"
CONF_CONSOLE_METRICS = "console_metrics"

DEFAULT_TITLE_ID = "PPSA99575"  # PS5LM
DEFAULT_WEB_PORT = 8080  # ps5-payload-websrv
DEFAULT_EXPORTER_PORT = 9100  # ps5-exporter, started by PS5LM
DEFAULT_API_PORT = 8081  # PS5LM's llama.cpp server (OpenAI API, /health, /metrics, /props)
DEFAULT_LLM_METRICS = True
DEFAULT_CONSOLE_METRICS = True
MIN_SCAN_INTERVAL = 10
DEFAULT_CONSOLE_INTERVAL = 120  # seconds between reads of the console exporter (each read stalls the model for a few seconds)
MIN_CONSOLE_INTERVAL = 30
DEFAULT_SCAN_INTERVAL = 30  # seconds

SERVICE_WAKE = "wake"
SERVICE_STANDBY = "standby"
SERVICE_START_APP = "start_app"
SERVICE_STOP_APP = "stop_app"

"""simulation-engine test path — must be first and only service on sys.path."""
import sys, os
from types import ModuleType, SimpleNamespace

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM  = os.path.join(REPO, "simulation-engine")
CORE = os.path.join(REPO, "core-engine")

# Remove core-engine from path to avoid models.schemas clash
if CORE in sys.path:
    sys.path.remove(CORE)

# Evict any core-engine modules already cached (e.g. models.schemas from core)
_core_prefix = CORE + os.sep
for _key in list(sys.modules):
    _mod = sys.modules[_key]
    _src = getattr(_mod, "__file__", None) or ""
    if _src.startswith(_core_prefix):
        del sys.modules[_key]

# Stub httpx (not installed in host Python — only available inside Docker images)
from unittest.mock import AsyncMock as _AsyncMock, MagicMock as _MagicMock
_httpx = ModuleType("httpx")
_httpx.AsyncClient = _MagicMock()
sys.modules["httpx"] = _httpx

# Replace any stale `config` stub (installed by core tests) with sim-compatible settings
_sim_settings = SimpleNamespace(
    session_close_hour=15,
    session_close_minute=20,
    redis_url="redis://localhost:6379",
    data_service_url="http://localhost:8003",
    core_engine_url="http://localhost:8001",
    log_level="INFO",
    sl_cooldown_minutes=30,
    min_option_premium=5.0,
    max_position_size_pct=10.0,
    slippage_pct=0.05,
    commission_flat=20.0,
    commission_pct=0.03,
    initial_capital=100000.0,
    slack_webhook_url="",
)
_cfg = ModuleType("config")
_cfg.settings = _sim_settings
sys.modules["config"] = _cfg

if SIM not in sys.path:
    sys.path.insert(0, SIM)

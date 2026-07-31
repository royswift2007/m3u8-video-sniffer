"""
Centralised tunable constants — all timeouts, size thresholds, and
performance knobs gathered in one place with compile-time defaults.

Every value CAN be overridden via ``config.json → tunables``;
the module reads ``utils.config_manager.config`` on first access,
falling back to the compile-time default when the key is missing
or the value is unparseable.

Usage::

    from utils.tunables import tunables
    grace = tunables.stop_terminate_grace_s

"""
from typing import Any


# ---------------------------------------------------------------------------
# Compile-time defaults (mirrored in the config schema below)
# ---------------------------------------------------------------------------
_DEFAULTS: dict[str, Any] = {
    # ---- CatCatch server ----
    "catcatch_port_range_start": 9527,
    "catcatch_port_range_end": 9539,
    "catcatch_bind_timeout_s": 5.0,
    "catcatch_body_max_bytes": 65536,
    # ---- Engine process lifecycle ----
    "stop_terminate_grace_s": 0.5,
    "stop_kill_deadline_s": 1.5,
    "pump_join_timeout_s": 0.25,
    "pump_put_timeout_s": 5.0,
    # ---- Download manager disk precheck ----
    "download_default_manifest_size_mib": 500,
    "download_disk_headroom_factor": 1.2,
    # ---- Playwright capture window (also in features, mirrored here) ----
    "browser_capture_window_seconds": 12,
    "browser_capture_extend_on_hit_seconds": 4,
    "browser_capture_probe_interval_ms": 1000,
    # ---- Browser / stale profile ----
    "browser_stale_profile_max_age_seconds": 86400,
    "browser_proc_wait_timeout_seconds": 10.0,
    # ---- Engine selector ----
    "engine_head_probe_timeout_ms": 500,
    # ---- HLS probe ----
    "hls_probe_timeout_s": 30.0,
    # ---- Worker pool ----
    "worker_soft_exit_timeout_s": 30.0,
}


class Tunables:
    """Namespace that reads tunable values from config, falling back to compile-time defaults."""

    _loaded: bool = False
    _cache: dict[str, Any] = {}

    @classmethod
    def _ensure_loaded(cls):
        if cls._loaded:
            return
        cls._loaded = True
        try:
            from utils.config_manager import config
            user_tunables = config.get("tunables", {}) or {}
            if isinstance(user_tunables, dict):
                for key, default_val in _DEFAULTS.items():
                    val = user_tunables.get(key)
                    if val is not None:
                        cls._cache[key] = val
                    else:
                        cls._cache[key] = default_val
            else:
                cls._cache.update(_DEFAULTS)
        except Exception:
            cls._cache.update(_DEFAULTS)

    def __getattr__(self, name: str) -> Any:
        Tunables._ensure_loaded()
        if name in Tunables._cache:
            return Tunables._cache[name]
        # Fall back to compile-time default if known
        if name in _DEFAULTS:
            return _DEFAULTS[name]
        raise AttributeError(f"Tunables has no key '{name}'")


# Module-level singleton
tunables = Tunables()

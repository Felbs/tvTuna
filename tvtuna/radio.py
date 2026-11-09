# SPDX-License-Identifier: GPL-3.0-or-later
"""One radio, many tenants.  The site warden (radio_lock.py: acquire / heartbeat / release /
should_yield / stop-file) decides who streams; tvTuna is one tenant among the sonde hunts,
the surveys and the soaks, at a human's priority.  A streaming receiver is never killed from
outside: it is asked (stop-file, CTRL_BREAK) so that IT closes the device, because a hard kill
of a streaming process wedges the vendor's API service."""
import importlib.util
from typing import Optional

from . import config

OWNER = "tvtuna"
PRIORITY = 60            # a human is watching; sonde passes (100) outrank us, surveys do not


def _module(cfg: config.Config):
    if not cfg.radio_lock:
        return None
    spec = importlib.util.spec_from_file_location("tvtuna_site_lock", cfg.radio_lock)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def holder(cfg: config.Config) -> Optional[dict]:
    """Who holds the radio now, or None (free, or no warden configured)."""
    mod = _module(cfg)
    if mod is None:
        return None
    try:
        return mod.status()
    except Exception:                                   # noqa: BLE001
        return None


def describe(st: Optional[dict]) -> str:
    if not st:
        return "radio free"
    return (f"radio held by {st.get('owner')} ({st.get('purpose', '?')}, priority {st.get('priority')}, "
            f"since {str(st.get('since', ''))[11:19]})")


def site_lock(cfg: config.Config, wait_s: float = 600.0, owner: str = OWNER, priority: int = PRIORITY):
    """rxtune's lock adapter over the warden: re-entrant, heartbeat throttled, yields on a
    stop request, a higher-priority waiter, or a lock that is no longer ours."""
    config.rxtune_on_path(cfg)
    from rxtune import lock as rxlock
    if not cfg.radio_lock:
        return rxlock.NullLock()
    return rxlock.ModuleLock(cfg.radio_lock, owner=owner, purpose="live TV", priority=priority, wait_s=wait_s)

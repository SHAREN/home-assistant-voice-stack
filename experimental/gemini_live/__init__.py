"""The Gemini Live integration."""

import logging
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DETAILED_LOGGING,
    DOMAIN,
    GEMINI_SESSION_MANAGER_KEY,
    GEMINI_TURN_STORE_KEY,
    OBSERVER_CARD_VERSION,
)
from . import stt, tts, conversation
from .runtime import LiveSessionManager, TurnStore
from .observer import async_setup_observer_history
from .utils import set_detailed_logging

_LOGGER = logging.getLogger(__name__)
CARD_RESOURCE_PATH = "/gemini_live/p610-live-observer-card.js"
CARD_MODULE_URL = f"{CARD_RESOURCE_PATH}?v={OBSERVER_CARD_VERSION}"
_FRONTEND_STATE_KEY = f"{DOMAIN}_frontend_state"

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Gemini Live from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    config = {**entry.data, **entry.options}
    set_detailed_logging(bool(config.get(CONF_DETAILED_LOGGING, False)))
    try:
        sdk_version = await hass.async_add_executor_job(version, "google-genai")
    except PackageNotFoundError:
        sdk_version = "not-installed"
    _LOGGER.info("Gemini Live google-genai version=%s", sdk_version)
    await _async_setup_frontend_once(hass)

    # Store configuration data (merging data and options)
    hass.data[DOMAIN][entry.entry_id] = {
        **entry.data,
        **entry.options,
        GEMINI_SESSION_MANAGER_KEY: LiveSessionManager(),
        GEMINI_TURN_STORE_KEY: TurnStore(),
    }

    # Register options update listener to reload integration when changed
    entry.async_on_unload(entry.add_update_listener(update_listener))

    # Forward setup to the platforms
    await hass.config_entries.async_forward_entry_setups(entry, ["stt", "tts", "conversation"])
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, ["stt", "tts", "conversation"]
    )
    if unload_ok:
        entry_data = hass.data[DOMAIN][entry.entry_id]
        await entry_data[GEMINI_SESSION_MANAGER_KEY].async_close_all()
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_setup_frontend_once(hass: HomeAssistant) -> None:
    """Register immutable static/frontend resources exactly once per Core run."""
    state = hass.data.setdefault(
        _FRONTEND_STATE_KEY,
        {"setup_task": None},
    )
    task = state.get("setup_task")
    if task is None:
        task = hass.async_create_task(
            _async_register_frontend(hass),
            "Register Gemini Live observer frontend",
        )
        state["setup_task"] = task
    try:
        await task
    except Exception:
        if state.get("setup_task") is task:
            state["setup_task"] = None
        raise


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Perform the single Core-lifetime frontend/history registration."""
    await async_setup_observer_history(hass)

    from homeassistant.components import frontend
    from homeassistant.components.http import StaticPathConfig

    await hass.http.async_register_static_paths(
        [StaticPathConfig("/gemini_live", str(Path(__file__).parent / "www"), True)]
    )
    frontend.add_extra_js_url(hass, CARD_MODULE_URL)

async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update by reloading the entry."""
    _LOGGER.debug("Gemini Live entry updated, reloading")
    await hass.config_entries.async_reload(entry.entry_id)

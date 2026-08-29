"""Config flow for Pipecat Assist."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_URL, DEFAULT_URL, DOMAIN

ADDON_NAME = "Pipecat Assist"
ADDON_SLUG_SUFFIX = "pipecat_assist"
ADDON_IMAGE_NAME = "pipecat-assist"
DEFAULT_ADDON_PORT = 7860
SUPERVISOR_URL = os.getenv("SUPERVISOR", "http://supervisor").rstrip("/")
if not SUPERVISOR_URL.startswith(("http://", "https://")):
    SUPERVISOR_URL = f"http://{SUPERVISOR_URL}"


async def _validate_url(hass: HomeAssistant, url: str) -> None:
    session = async_get_clientsession(hass)
    async with asyncio.timeout(10):
        async with session.get(f"{url.rstrip('/')}/api/assist/status") as response:
            if response.status >= 400:
                raise aiohttp.ClientError(f"Unexpected status {response.status}")


def _payload_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _matches_pipecat_addon(addon: dict[str, Any]) -> bool:
    slug = str(addon.get("slug", "")).lower()
    name = str(addon.get("name", "")).lower()
    image = str(addon.get("image", "")).lower()
    repository = str(addon.get("repository", "")).lower()
    return (
        slug.endswith(ADDON_SLUG_SUFFIX)
        or name == ADDON_NAME.lower()
        or ADDON_IMAGE_NAME in image
        or "pipecat-homeassistant" in repository
    )


def _addon_port(addon: dict[str, Any]) -> int:
    options = addon.get("options") if isinstance(addon.get("options"), dict) else {}
    for value in (
        options.get("runner_port"),
        addon.get("ingress_port"),
        addon.get("port"),
        DEFAULT_ADDON_PORT,
    ):
        try:
            port = int(value)
        except (TypeError, ValueError):
            continue
        if port > 0:
            return port
    return DEFAULT_ADDON_PORT


def _addon_url_candidates(addon: dict[str, Any]) -> list[str]:
    slug = str(addon.get("slug", "")).strip()
    hostname = str(addon.get("hostname", "")).strip()
    port = _addon_port(addon)
    hosts = [hostname, slug, "127.0.0.1", "localhost"]
    return [f"http://{host}:{port}" for host in dict.fromkeys(hosts) if host]


async def _supervisor_addon_info(hass: HomeAssistant) -> dict[str, Any] | None:
    token = os.getenv("SUPERVISOR_TOKEN", "")
    if not token:
        return None

    session = async_get_clientsession(hass)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with asyncio.timeout(10):
            async with session.get(f"{SUPERVISOR_URL}/addons", headers=headers) as response:
                if response.status >= 400:
                    return None
                payload = _payload_data(await response.json())
    except (TimeoutError, aiohttp.ClientError):
        return None

    addons = payload.get("addons")
    if not isinstance(addons, list):
        return None

    addon = next(
        (
            item
            for item in addons
            if isinstance(item, dict) and _matches_pipecat_addon(item)
        ),
        None,
    )
    if not addon:
        return None

    slug = str(addon.get("slug", "")).strip()
    if not slug:
        return addon

    try:
        async with asyncio.timeout(10):
            info_url = f"{SUPERVISOR_URL}/addons/{slug}/info"
            async with session.get(info_url, headers=headers) as response:
                if response.status < 400:
                    info = _payload_data(await response.json())
                    if isinstance(info, dict):
                        return {**addon, **info, "slug": slug}
    except (TimeoutError, aiohttp.ClientError):
        pass
    return addon


async def _suggest_addon_url(hass: HomeAssistant) -> str:
    addon = await _supervisor_addon_info(hass)
    candidates = _addon_url_candidates(addon) if addon else [DEFAULT_URL]
    for candidate in candidates:
        try:
            await _validate_url(hass, candidate)
        except (TimeoutError, aiohttp.ClientError):
            continue
        return candidate
    return candidates[0] if candidates else DEFAULT_URL


class PipecatAssistConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Pipecat Assist config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""

        errors: dict[str, str] = {}
        suggested_url = (
            user_input.get(CONF_URL, "")
            if user_input
            else await _suggest_addon_url(self.hass)
        )

        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            try:
                await _validate_url(self.hass, url)
            except (TimeoutError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(url)
                self._abort_if_unique_id_configured()
                data = {CONF_URL: url}
                return self.async_create_entry(title="Pipecat Assist", data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=suggested_url or DEFAULT_URL): str,
                }
            ),
            errors=errors,
        )

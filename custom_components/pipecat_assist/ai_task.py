"""AI Task entity for Pipecat Assist."""

from __future__ import annotations

import base64
import binascii
from typing import Any

import aiohttp

from homeassistant.components import ai_task, conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_FLOW_ID, CONF_TOKEN, CONF_URL

SUPPORTED_FEATURES = ai_task.AITaskEntityFeature.GENERATE_DATA
if hasattr(ai_task.AITaskEntityFeature, "GENERATE_IMAGE"):
    SUPPORTED_FEATURES |= ai_task.AITaskEntityFeature.GENERATE_IMAGE


async def _response_payload(response: aiohttp.ClientResponse) -> dict[str, Any]:
    """Return JSON when available, falling back to response text."""

    try:
        data = await response.json()
        return data if isinstance(data, dict) else {"detail": str(data)}
    except (aiohttp.ContentTypeError, ValueError):
        return {"detail": await response.text()}


def _structure_fields(structure: Any) -> list[dict[str, Any]]:
    """Return a compact, JSON-safe description of a voluptuous structure."""

    raw_schema = getattr(structure, "schema", None)
    if not isinstance(raw_schema, dict):
        return []

    fields: list[dict[str, Any]] = []
    for key, selector in raw_schema.items():
        name = str(getattr(key, "schema", key))
        required = key.__class__.__name__.lower() == "required"
        description = str(getattr(key, "description", "") or "")
        fields.append(
            {
                "name": name,
                "required": required,
                "description": description,
                "selector": selector.__class__.__name__,
            }
        )
    return fields


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Pipecat Assist AI Tasks."""

    async_add_entities([PipecatAssistAITaskEntity(hass, entry)])


class PipecatAssistAITaskEntity(ai_task.AITaskEntity):
    """AI Task entity backed by the Pipecat Assist add-on."""

    _attr_has_entity_name = True
    _attr_name = "Pipecat Assist"
    _attr_supported_features = SUPPORTED_FEATURES

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ai_task"
        self._session = async_get_clientsession(hass)

    async def _async_generate_data(
        self,
        task: ai_task.GenDataTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenDataTaskResult:
        """Handle a generate data task."""

        url = self._entry.data[CONF_URL].rstrip("/")
        token = self._entry.data.get(CONF_TOKEN)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        payload: dict[str, Any] = {
            "task_name": task.name,
            "instructions": task.instructions,
            "structured": bool(task.structure),
            "structure_fields": _structure_fields(task.structure),
            "conversation_id": chat_log.conversation_id,
        }
        if flow_id := self._entry.data.get(CONF_FLOW_ID):
            payload["flow_id"] = flow_id

        try:
            async with self._session.post(
                f"{url}/api/assist/ai-task",
                json=payload,
                headers=headers,
            ) as response:
                data = await _response_payload(response)
                if response.status >= 400:
                    raise HomeAssistantError(
                        data.get("detail") or "Pipecat Assist AI Task failed"
                    )
        except aiohttp.ClientError as err:
            raise HomeAssistantError(f"Pipecat Assist is not reachable: {err}") from err

        return ai_task.GenDataTaskResult(
            conversation_id=data.get("conversation_id") or chat_log.conversation_id,
            data=data.get("data"),
        )

    async def _async_generate_image(
        self,
        task: ai_task.GenImageTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenImageTaskResult:
        """Handle a generate image task."""

        url = self._entry.data[CONF_URL].rstrip("/")
        token = self._entry.data.get(CONF_TOKEN)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        payload: dict[str, Any] = {
            "task_name": task.name,
            "instructions": task.instructions,
            "conversation_id": chat_log.conversation_id,
        }
        if flow_id := self._entry.data.get(CONF_FLOW_ID):
            payload["flow_id"] = flow_id

        try:
            async with self._session.post(
                f"{url}/api/assist/ai-task/image",
                json=payload,
                headers=headers,
            ) as response:
                data = await _response_payload(response)
                if response.status >= 400:
                    raise HomeAssistantError(
                        data.get("detail") or "Pipecat Assist image generation failed"
                    )
        except aiohttp.ClientError as err:
            raise HomeAssistantError(f"Pipecat Assist is not reachable: {err}") from err

        try:
            image_data = base64.b64decode(str(data.get("image_base64") or ""), validate=True)
        except (binascii.Error, ValueError) as err:
            raise HomeAssistantError("Pipecat Assist returned invalid image data") from err
        if not image_data:
            raise HomeAssistantError("Pipecat Assist returned an empty image")

        return ai_task.GenImageTaskResult(
            conversation_id=data.get("conversation_id") or chat_log.conversation_id,
            image_data=image_data,
            mime_type=data.get("mime_type") or "image/png",
            width=data.get("width"),
            height=data.get("height"),
            model=data.get("model"),
            revised_prompt=data.get("revised_prompt"),
        )

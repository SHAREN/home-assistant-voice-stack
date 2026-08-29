"""Home Assistant MCP bridge for Pipecat and text requests."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from mcp.client.session_group import StreamableHttpParameters

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import LLMService
from pipecat.services.mcp_service import MCPClient

from app.conversation_log import CONVERSATION_LOGS


class MCPAuthenticationError(RuntimeError):
    """Raised when Home Assistant rejects the MCP bearer token."""


MCP_CALL_HISTORY: deque[dict[str, Any]] = deque(maxlen=100)
MCP_TOOLS_SCHEMA_CACHE: dict[tuple[str, tuple[str, ...]], tuple[ToolsSchema, float]] = {}
LLM_SCHEMA_SCALAR_TYPES = {"string", "number", "integer", "boolean", "object", "array"}
LLM_SCHEMA_STRING_FIELDS = {"description", "format", "title"}


MCP_MODEL_RESULT_LIMIT = 16000
ENTITY_SEARCH_FIELDS = [
    "entities",
    "entity_total_matches",
    "count",
    "has_more",
    "next_offset",
    "warnings",
    "partial",
    "partial_reason",
]
ENTITY_SEARCH_RESULT_FIELDS = [
    "entity_id",
    "friendly_name",
    "domain",
    "state",
    "area",
    "aliases",
]
READ_ONLY_GENERIC_TOOL_PREFIXES = (
    "ha_get_",
    "ha_list_",
    "ha_search",
)


def _is_read_only_generic_tool(name: str) -> bool:
    short_name = str(name or "").rsplit("__", 1)[-1]
    return any(short_name.startswith(prefix) for prefix in READ_ONLY_GENERIC_TOOL_PREFIXES)


def _prepare_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Normalize model mistakes and enforce compact/read-only MCP calls."""

    prepared = dict(arguments or {})

    if str(name).endswith("ha_call_tool"):
        # Gemini occasionally wraps the generic call tool inside itself:
        # ha_call_tool(name=ha_call_tool, arguments={name=ha_get_history, ...}).
        # Unwrap at most two accidental layers, then enforce a read-only target.
        for _ in range(2):
            target = str(prepared.get("name") or "")
            inner = prepared.get("arguments")
            if target.endswith("ha_call_tool") and isinstance(inner, dict):
                inner_name = str(inner.get("name") or "")
                if inner_name:
                    logger.warning(
                        "Unwrapped nested generic MCP call {} -> {}",
                        target,
                        inner_name,
                    )
                    prepared = {
                        "name": inner_name,
                        "arguments": dict(inner.get("arguments") or {}),
                    }
                    continue
            break

        target = str(prepared.get("name") or "")
        if not target:
            raise ValueError("The generic MCP call is missing a target tool name")
        if not _is_read_only_generic_tool(target):
            raise PermissionError(
                f"Voice access to MCP tool '{target}' is blocked; only read-only get/list/search tools are allowed"
            )
        if not isinstance(prepared.get("arguments"), dict):
            prepared["arguments"] = {}

    if str(name).endswith("ha_search") and not prepared.get("search_types"):
        prepared.setdefault("limit", 10)
        prepared.setdefault("include_config", False)
        prepared.setdefault("fields", ENTITY_SEARCH_FIELDS)
        prepared.setdefault("result_fields", ENTITY_SEARCH_RESULT_FIELDS)
    return prepared


def _model_tool_result(name: str, raw_result: str) -> tuple[str, bool, str]:
    """Return a bounded model-facing tool result and classify textual tool failures."""

    text = str(raw_result or "").strip() or "Tool returned no text content."
    lowered = text.lower()
    is_error = lowered.startswith("error calling tool:") or lowered.startswith("tool error:")
    parsed: Any = None
    with suppress(Exception):
        parsed = json.loads(text)
        if isinstance(parsed, str):
            parsed = json.loads(parsed)
    if isinstance(parsed, dict):
        is_error = is_error or parsed.get("success") is False or bool(parsed.get("error")) or parsed.get("isError") is True
    if "unknown tool" in lowered:
        is_error = True
    if is_error:
        # Home Assistant/MCP can return failures as successful text content.
        # Normalize them so the model and conversation audit see a real failure.
        if isinstance(parsed, dict) and parsed.get("error"):
            compact_error = str(parsed.get("error"))[:4000]
        else:
            compact_error = text[:4000]
        return json.dumps({"success": False, "error": compact_error}, ensure_ascii=False), False, compact_error
    if len(text) <= MCP_MODEL_RESULT_LIMIT:
        return text, True, ""
    preview = text[: MCP_MODEL_RESULT_LIMIT - 800]
    compact = json.dumps(
        {
            "success": True,
            "truncated": True,
            "message": "Tool result was shortened by Pipecat Assist to protect the realtime context.",
            "result_preview": preview,
        },
        ensure_ascii=False,
    )
    logger.warning(
        "Truncated MCP result for {} from {} to {} characters",
        name,
        len(text),
        len(compact),
    )
    return compact, True, ""


def _compact_json(value: Any, limit: int = 1200) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        except TypeError:
            text = str(value)
    if len(text) > limit:
        return f"{text[:limit]}..."
    return text


def list_mcp_call_history() -> dict[str, Any]:
    """Return recent Home Assistant MCP tool calls."""

    return {"calls": list(reversed(MCP_CALL_HISTORY))}


def clear_mcp_call_history() -> dict[str, Any]:
    """Clear recent Home Assistant MCP tool calls."""

    MCP_CALL_HISTORY.clear()
    return list_mcp_call_history()


def clear_mcp_tools_cache() -> None:
    """Clear cached MCP tool schemas."""

    MCP_TOOLS_SCHEMA_CACHE.clear()


def _schema_type(value: Any) -> tuple[str, bool]:
    if isinstance(value, str):
        return (value.lower(), value.lower() == "null")
    if isinstance(value, list):
        nullable = any(str(item).lower() == "null" for item in value)
        for item in value:
            item_type = str(item).lower()
            if item_type != "null":
                return item_type, nullable
        return "", nullable
    return "", False


def _sanitize_llm_schema(schema: Any) -> dict[str, Any]:
    """Return a Gemini/Pipecat-compatible subset of JSON Schema."""

    if not isinstance(schema, dict):
        return {"type": "string"}

    sanitized: dict[str, Any] = {}
    schema_type, nullable = _schema_type(schema.get("type"))
    if schema_type in LLM_SCHEMA_SCALAR_TYPES:
        sanitized["type"] = schema_type
    elif isinstance(schema.get("properties"), dict) or "additionalProperties" in schema or "propertyNames" in schema:
        sanitized["type"] = "object"
    elif isinstance(schema.get("items"), dict):
        sanitized["type"] = "array"

    for key in LLM_SCHEMA_STRING_FIELDS:
        value = schema.get(key)
        if isinstance(value, str) and value:
            sanitized[key] = value

    enum_values = schema.get("enum")
    if isinstance(enum_values, list):
        clean_enum = [
            value
            for value in enum_values
            if isinstance(value, str | int | float | bool) or value is None
        ]
        if clean_enum:
            sanitized["enum"] = clean_enum

    if "const" in schema and "enum" not in sanitized:
        const = schema.get("const")
        if isinstance(const, str | int | float | bool) or const is None:
            sanitized["enum"] = [const]

    properties = schema.get("properties")
    if isinstance(properties, dict):
        clean_properties = {
            str(name): _sanitize_llm_schema(value)
            for name, value in properties.items()
            if isinstance(name, str)
        }
        if clean_properties:
            sanitized["properties"] = clean_properties

    required = schema.get("required")
    if isinstance(required, list):
        allowed_names = set(sanitized.get("properties", {}).keys())
        clean_required = [
            name
            for name in required
            if isinstance(name, str) and (not allowed_names or name in allowed_names)
        ]
        if clean_required:
            sanitized["required"] = clean_required

    items = schema.get("items")
    if isinstance(items, dict):
        sanitized["items"] = _sanitize_llm_schema(items)

    any_of_items = schema.get("anyOf") or schema.get("any_of") or schema.get("oneOf") or schema.get("allOf")
    if isinstance(any_of_items, list):
        clean_any_of: list[dict[str, Any]] = []
        for item in any_of_items:
            item_type, item_nullable = _schema_type(item.get("type") if isinstance(item, dict) else None)
            nullable = nullable or item_nullable or item_type == "null"
            if item_type == "null":
                continue
            if isinstance(item, dict):
                clean_any_of.append(_sanitize_llm_schema(item))
        if clean_any_of:
            sanitized["anyOf"] = clean_any_of

    if isinstance(schema.get("nullable"), bool) or nullable:
        sanitized["nullable"] = bool(schema.get("nullable") or nullable)

    if not sanitized:
        return {"type": "string"}
    return sanitized


def _sanitize_tool_properties(properties: Any) -> dict[str, Any]:
    if not isinstance(properties, dict):
        return {}
    return {
        str(name): _sanitize_llm_schema(schema)
        for name, schema in properties.items()
        if isinstance(name, str)
    }


def _sanitize_function_schema(tool: FunctionSchema) -> FunctionSchema:
    properties = _sanitize_tool_properties(tool.properties)
    required = [
        name
        for name in (tool.required or [])
        if isinstance(name, str) and (not properties or name in properties)
    ]
    return FunctionSchema(
        name=tool.name,
        description=tool.description,
        properties=properties,
        required=required,
    )


def _sanitize_tools_schema(tools: ToolsSchema) -> ToolsSchema:
    return ToolsSchema(
        standard_tools=[_sanitize_function_schema(tool) for tool in tools.standard_tools]
    )


def _new_history_item(
    name: str,
    arguments: dict[str, Any],
    conversation_session_id: str = "",
) -> tuple[dict[str, Any], float]:
    return (
        {
            "id": uuid.uuid4().hex[:12],
            "started_at": datetime.now(timezone.utc).isoformat(),
            "tool": name,
            "arguments": _compact_json(arguments),
            "_arguments_raw": dict(arguments or {}),
            "_conversation_session_id": conversation_session_id,
        },
        time.perf_counter(),
    )


def _finish_history_item(
    history_item: dict[str, Any],
    started: float,
    *,
    ok: bool,
    result: str = "",
    error: str = "",
) -> None:
    duration_ms = round((time.perf_counter() - started) * 1000)
    conversation_session_id = str(history_item.pop("_conversation_session_id", "") or "")
    raw_arguments = history_item.pop("_arguments_raw", {})
    history_item.update(
        {
            "ok": ok,
            "duration_ms": duration_ms,
        }
    )
    if ok:
        history_item["result"] = _compact_json(result, limit=1000)
    else:
        history_item["error"] = error or result or "MCP tool failed"
    MCP_CALL_HISTORY.append(history_item)
    if conversation_session_id:
        CONVERSATION_LOGS.record_tool_call(
            conversation_session_id,
            tool=str(history_item.get("tool") or "unknown"),
            arguments=dict(raw_arguments or {}),
            ok=ok,
            duration_ms=duration_ms,
            result=result,
            error=error,
        )


class RecordingMCPClient(MCPClient):
    """Pipecat MCP client that records tool calls for the Runtime UI and session log."""

    def __init__(self, *args, conversation_session_id: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.conversation_session_id = conversation_session_id

    async def _call_tool(self, session, function_name, arguments, result_callback):
        history_item, started = _new_history_item(
            function_name,
            dict(arguments or {}),
            self.conversation_session_id,
        )
        logger.debug("Calling mcp tool '{}'", function_name)
        results = None
        error = ""
        try:
            results = await session.call_tool(function_name, arguments=arguments)
        except Exception as err:
            error = f"Error calling mcp tool {function_name}: {err}"
            logger.error(error)

        response = ""
        if results:
            if hasattr(results, "content") and results.content:
                for index, content in enumerate(results.content):
                    if hasattr(content, "text") and content.text:
                        logger.debug("Tool response chunk {}: {}", index, content.text)
                        response += content.text
            else:
                logger.error("Error getting content from {} results.", function_name)

        if function_name in self._tools_output_filters:
            try:
                response = self._tools_output_filters[function_name](response)
                logger.debug("Final response after filter: {}", response)
            except Exception:
                logger.error("Error applying output filter for {}", function_name)
                response = ""

        ok = bool(response and isinstance(response, str) and not error)
        if ok:
            logger.info("Tool '{}' completed successfully", function_name)
            logger.debug("Final response: {}", response)
        else:
            response = "Sorry, could not call the mcp tool"

        _finish_history_item(
            history_item,
            started,
            ok=ok,
            result=response,
            error=error,
        )
        await result_callback(response)


def _tool_prefix(value: str) -> str:
    prefix = "".join(char if char.isalnum() else "_" for char in value.lower()).strip("_")
    return prefix or "mcp"


class HomeAssistantMCPBridge:
    """Small wrapper around Pipecat's MCPClient."""

    def __init__(
        self,
        url: str,
        token: str = "",
        tool_allowlist: Sequence[str] | None = None,
        *,
        name: str = "Home Assistant MCP",
        conversation_session_id: str = "",
    ):
        self.url = url
        self.token = token
        self.tool_allowlist = list(tool_allowlist or [])
        self.name = name
        self.conversation_session_id = conversation_session_id
        self.client: MCPClient | None = None

    async def __aenter__(self) -> "HomeAssistantMCPBridge":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def start(self) -> None:
        """Connect to Home Assistant MCP."""

        if self.client:
            return
        await self._preflight_auth()
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self.client = RecordingMCPClient(
            server_params=StreamableHttpParameters(
                url=self.url,
                headers=headers,
            ),
            tools_filter=self.tool_allowlist or None,
            conversation_session_id=self.conversation_session_id,
        )
        await self.client.start()
        logger.info("Connected to {} at {}", self.name, self.url)

    async def _preflight_auth(self) -> None:
        """Detect auth failures before MCPClient starts background tasks."""

        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        payload = {"jsonrpc": "2.0", "id": "pipecat-assist-preflight", "method": "ping"}

        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                response = await client.post(self.url, headers=headers, json=payload)
        except httpx.HTTPError as err:
            raise RuntimeError(f"Home Assistant MCP is not reachable: {err}") from err

        if response.status_code in {401, 403}:
            raise MCPAuthenticationError(
                "Home Assistant MCP rejected the token. The add-on normally uses "
                "the Home Assistant Supervisor token automatically. If you are "
                "running outside the Supervisor or using a custom MCP URL, configure "
                "a long-lived access token."
            )
        if response.status_code == 404:
            raise RuntimeError(f"Home Assistant MCP endpoint was not found at {self.url}")
        if response.status_code >= 500:
            raise RuntimeError(
                f"Home Assistant MCP returned HTTP {response.status_code}. Check the Home Assistant logs."
            )

    async def close(self) -> None:
        """Close the MCP connection."""

        client = self.client
        self.client = None
        if not client:
            return
        try:
            await client.close()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.debug("Ignoring MCP close error: {}", err)

    async def tools_schema(
        self,
        *,
        cache_enabled: bool = True,
        cache_ttl_seconds: int = 300,
        refresh: bool = False,
    ) -> ToolsSchema:
        """Return MCP tools in Pipecat schema format."""

        if not self.client:
            raise RuntimeError("MCP bridge is not started")
        cache_key = (self.url, tuple(self.tool_allowlist))
        cached = MCP_TOOLS_SCHEMA_CACHE.get(cache_key)
        now = time.time()
        if (
            cache_enabled
            and not refresh
            and cached
            and (cache_ttl_seconds <= 0 or now - cached[1] <= cache_ttl_seconds)
        ):
            logger.debug("Using cached MCP tool schema for {} at {}", self.name, self.url)
            return cached[0]

        tools = _sanitize_tools_schema(await self.client.get_tools_schema())
        if cache_enabled:
            MCP_TOOLS_SCHEMA_CACHE[cache_key] = (tools, now)
        return tools

    async def register_tools_schema(self, tools: ToolsSchema, llm: LLMService) -> None:
        """Register MCP tools with a Pipecat LLM service."""

        if not self.client:
            raise RuntimeError("MCP bridge is not started")
        await self.client.register_tools_schema(tools, llm)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call one MCP tool and return compact model-facing text content."""

        if not self.client:
            raise RuntimeError("MCP bridge is not started")
        try:
            prepared_arguments = _prepare_tool_arguments(name, arguments)
        except (ValueError, PermissionError) as err:
            history_item, started = _new_history_item(
                name,
                dict(arguments or {}),
                self.conversation_session_id,
            )
            error = str(err)
            _finish_history_item(history_item, started, ok=False, error=error)
            logger.warning("Blocked invalid or unsafe MCP call {}: {}", name, error)
            return json.dumps({"success": False, "error": error}, ensure_ascii=False)

        history_item, started = _new_history_item(
            name,
            prepared_arguments,
            self.conversation_session_id,
        )
        try:
            session = self.client._ensure_connected()  # Pipecat exposes no public call_tool yet.
            result = await session.call_tool(name, arguments=prepared_arguments)
            chunks: list[str] = []
            for content in getattr(result, "content", []) or []:
                text = getattr(content, "text", None)
                if text:
                    chunks.append(text)
            raw_result = "\n".join(chunks) if chunks else "Tool returned no text content."
            model_result, ok, error = _model_tool_result(name, raw_result)
            if ok:
                _finish_history_item(history_item, started, ok=True, result=raw_result)
            else:
                _finish_history_item(history_item, started, ok=False, error=error, result=raw_result)
            return model_result
        except Exception as err:
            _finish_history_item(history_item, started, ok=False, error=str(err))
            raise



class CombinedMCPBridge:
    """Expose multiple MCP servers as one tool surface."""

    def __init__(
        self,
        servers: Sequence[dict[str, Any]],
        tool_allowlist: Sequence[str] | None = None,
        conversation_session_id: str = "",
    ):
        self.server_specs = list(servers)
        self.tool_allowlist = list(tool_allowlist or [])
        self.conversation_session_id = conversation_session_id
        self.bridges: list[tuple[dict[str, Any], HomeAssistantMCPBridge]] = []
        self._tool_routes: dict[str, tuple[HomeAssistantMCPBridge, str]] = {}
        self._tools_schema: ToolsSchema | None = None

    async def __aenter__(self) -> "CombinedMCPBridge":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def start(self) -> None:
        """Connect all configured MCP servers."""

        if self.bridges:
            return
        errors: list[str] = []
        for spec in self.server_specs:
            bridge = HomeAssistantMCPBridge(
                str(spec.get("url") or ""),
                str(spec.get("token") or ""),
                self.tool_allowlist,
                name=str(spec.get("name") or spec.get("id") or "MCP Server"),
                conversation_session_id=self.conversation_session_id,
            )
            try:
                await bridge.start()
            except Exception as err:
                errors.append(f"{bridge.name}: {err}")
                with suppress(Exception):
                    await bridge.close()
                continue
            self.bridges.append((spec, bridge))

        if errors:
            for _, bridge in self.bridges:
                with suppress(Exception):
                    await bridge.close()
            self.bridges = []
            raise RuntimeError("; ".join(errors))
        if not self.bridges:
            raise RuntimeError("; ".join(errors) or "No MCP servers are configured")

    async def close(self) -> None:
        """Close all MCP connections."""

        bridges = self.bridges
        self.bridges = []
        self._tool_routes = {}
        self._tools_schema = None
        for _, bridge in bridges:
            with suppress(Exception):
                await bridge.close()

    async def tools_schema(
        self,
        *,
        cache_enabled: bool = True,
        cache_ttl_seconds: int = 300,
        refresh: bool = False,
    ) -> ToolsSchema:
        """Return a combined Pipecat tools schema."""

        if not self.bridges:
            raise RuntimeError("MCP bridge is not started")

        public_tools: list[FunctionSchema] = []
        routes: dict[str, tuple[HomeAssistantMCPBridge, str]] = {}
        seen: set[str] = set()

        for spec, bridge in self.bridges:
            schema = await bridge.tools_schema(
                cache_enabled=cache_enabled,
                cache_ttl_seconds=cache_ttl_seconds,
                refresh=refresh,
            )
            prefix = _tool_prefix(str(spec.get("id") or bridge.name))
            prefer_unprefixed = bool(spec.get("prefer_unprefixed"))
            for tool in schema.standard_tools:
                original_name = tool.name
                public_name = original_name
                if not prefer_unprefixed or public_name in seen:
                    public_name = f"{prefix}__{original_name}"
                if public_name in seen:
                    public_name = f"{prefix}_{len(seen)}__{original_name}"
                seen.add(public_name)
                routes[public_name] = (bridge, original_name)
                public_properties = _sanitize_tool_properties(tool.properties)
                public_tools.append(
                    FunctionSchema(
                        name=public_name,
                        description=f"{bridge.name}: {tool.description}",
                        properties=public_properties,
                        required=[
                            name
                            for name in (tool.required or [])
                            if isinstance(name, str) and name in public_properties
                        ],
                    )
                )

        self._tool_routes = routes
        self._tools_schema = ToolsSchema(standard_tools=public_tools)
        return self._tools_schema

    async def register_tools_schema(self, tools: ToolsSchema, llm: LLMService) -> None:
        """Register combined MCP callbacks with an LLM service."""

        if not self._tool_routes:
            await self.tools_schema()

        for public_name in self._tool_routes:
            async def handler(params, *, name=public_name) -> None:
                result = await self.call_tool(name, dict(params.arguments or {}))
                await params.result_callback(result)

            llm.register_function(public_name, handler)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call a routed MCP tool."""

        if not self._tool_routes:
            await self.tools_schema()
        route = self._tool_routes.get(name)
        if not route:
            raise RuntimeError(f"Unknown MCP tool: {name}")
        bridge, original_name = route
        return await bridge.call_tool(original_name, arguments)


async def check_mcp(
    url: str,
    token: str,
    tool_allowlist: Sequence[str] | None = None,
    *,
    cache_enabled: bool = True,
    cache_ttl_seconds: int = 300,
    refresh: bool = False,
) -> dict[str, Any]:
    """Probe MCP connectivity for the status endpoint."""

    try:
        async with HomeAssistantMCPBridge(url, token, tool_allowlist) as bridge:
            tools = await bridge.tools_schema(
                cache_enabled=cache_enabled,
                cache_ttl_seconds=cache_ttl_seconds,
                refresh=refresh,
            )
            return {
                "ok": True,
                "tool_count": len(tools.standard_tools),
                "tools": [tool.name for tool in tools.standard_tools[:50]],
            }
    except asyncio.CancelledError as err:
        logger.warning("MCP check was cancelled: {}", err)
        return {"ok": False, "error": "MCP check was cancelled", "tool_count": 0, "tools": []}
    except Exception as err:
        logger.warning("MCP check failed: {}", err)
        return {"ok": False, "error": str(err), "tool_count": 0, "tools": []}

"""Persistent conversation and MCP tool-call logs for Pipecat Assist."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from loguru import logger


LOG_ROOT = Path(os.getenv("CONVERSATION_LOG_DIR", "/data/conversation_logs"))
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "password",
    "secret",
    "secret_key",
    "token",
    "access_token",
    "refresh_token",
    "longlived_token",
}
_LOOKUP_MISS_PATTERNS = (
    re.compile(r"No exposed entities matched name ['\"](?P<query>[^'\"]+)['\"]", re.I),
    re.compile(r"No exposed entities found in domain", re.I),
    re.compile(r"Area ['\"](?P<query>[^'\"]+)['\"] does not exist", re.I),
    re.compile(r"not found", re.I),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_component(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip()).strip("-.")
    return clean[:80] or "session"


def _redact(value: Any, key: str = "") -> Any:
    """Convert arbitrary values to JSON-safe values and redact common secrets."""

    if key.lower() in _SECRET_KEYS:
        return "[REDACTED]"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return f"[bytes:{len(value)}]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _redact(model_dump())
        except Exception:
            pass
    return str(value)


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("transcript")
                if text:
                    parts.append(str(text))
        return "\n".join(part.strip() for part in parts if part.strip()).strip()
    if isinstance(value, dict):
        text = value.get("text") or value.get("content") or value.get("transcript")
        return str(text or "").strip()
    return str(value or "").strip()


def _normalise_messages(messages: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(messages, list):
        return result
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            continue
        clean = _redact(item)
        role = str(clean.get("role") or "unknown")
        content = _text_content(clean.get("content"))
        entry: dict[str, Any] = {
            "index": index,
            "role": role,
            "content": content,
        }
        for name in ("name", "tool_call_id", "tool_calls", "function_call"):
            if clean.get(name) not in (None, "", [], {}):
                entry[name] = clean.get(name)
        result.append(entry)
    return result


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _is_lookup_miss(result: str, error: str = "") -> tuple[bool, str]:
    text = f"{result}\n{error}".strip()
    for pattern in _LOOKUP_MISS_PATTERNS:
        match = pattern.search(text)
        if match:
            return True, str(match.groupdict().get("query") or "").strip()
    try:
        data = json.loads(result)
    except Exception:
        data = None
    if isinstance(data, dict) and data.get("success") is False:
        return True, str(data.get("error") or "").strip()
    return False, ""


def _candidate_entity_names(result: str) -> list[str]:
    names: list[str] = []
    for line in str(result or "").splitlines():
        match = re.match(r"^\s*-?\s*names:\s*(.+?)\s*$", line, re.I)
        if not match:
            continue
        value = match.group(1).strip().strip("'\"")
        if value and value not in names:
            names.append(value)
    return names


def _name_similarity(query: str, candidate: str) -> float:
    def tokens(value: str) -> set[str]:
        return {
            item
            for item in re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]+", value.lower())
            if len(item) > 1
        }
    query_tokens = tokens(query)
    candidate_tokens = tokens(candidate)
    if not query_tokens or not candidate_tokens:
        return 0.0
    return len(query_tokens & candidate_tokens) / len(query_tokens)


def _derive_analysis_issues(metadata: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues = [dict(item) for item in (metadata.get("analysis_issues") or []) if isinstance(item, dict)]
    tool_events = [event for event in events if event.get("event") == "mcp_tool_call"]
    existing = {(item.get("type"), item.get("query"), item.get("candidate")) for item in issues}
    for index, event in enumerate(tool_events):
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if not data.get("lookup_miss"):
            continue
        query = str(data.get("lookup_query") or "").strip()
        if not query:
            arguments = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
            query = str(arguments.get("name") or arguments.get("area") or "").strip()
        if not query:
            continue
        candidates: list[str] = []
        for later in tool_events[index + 1 :]:
            later_data = later.get("data") if isinstance(later.get("data"), dict) else {}
            candidates.extend(_candidate_entity_names(str(later_data.get("result") or "")))
        ranked = sorted(
            ((candidate, _name_similarity(query, candidate)) for candidate in dict.fromkeys(candidates)),
            key=lambda item: item[1],
            reverse=True,
        )
        if not ranked or ranked[0][1] < 0.5:
            continue
        candidate, score = ranked[0]
        key = ("alias_suggestion", query, candidate)
        if key in existing:
            continue
        existing.add(key)
        issues.append(
            {
                "type": "alias_suggestion",
                "query": query,
                "candidate": candidate,
                "confidence": round(score, 2),
                "message": (
                    f"После неудачного запроса «{query}» найдено полное имя «{candidate}». "
                    f"Вероятно, стоит добавить «{query}» как alias этой сущности."
                ),
            }
        )
    return issues


def _markdown(session: dict[str, Any], messages: list[dict[str, Any]], events: list[dict[str, Any]]) -> str:
    title = session.get("title") or session.get("flow_name") or "Pipecat conversation"
    lines = [
        f"# {title}",
        "",
        f"- Session: `{session.get('id', '')}`",
        f"- Started: {session.get('started_at', '')}",
        f"- Finished: {session.get('finished_at') or 'in progress'}",
        f"- Status: {session.get('status', 'running')}",
        f"- Source: {session.get('source', '')}",
        f"- Client: `{session.get('client_id', '')}`",
        f"- Flow: `{session.get('flow_id', '')}`",
        f"- Provider/model: {session.get('provider', '')} / {session.get('model', '')}",
        "",
        "## Dialogue",
        "",
    ]
    dialogue = [m for m in messages if m.get("role") not in {"system", "developer", "tool"} and m.get("content")]
    if not dialogue:
        lines.append("_No completed dialogue messages yet._")
    for message in dialogue:
        role = message.get("role", "unknown")
        label = {"user": "User", "assistant": "Assistant"}.get(role, role.title())
        lines.extend([f"### {label}", "", str(message.get("content") or ""), ""])

    tool_events = [event for event in events if event.get("event") == "mcp_tool_call"]
    lines.extend(["## MCP tool calls", ""])
    if not tool_events:
        lines.append("_No MCP calls._")
    for event in tool_events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        semantic = "lookup miss" if data.get("lookup_miss") else ("ok" if data.get("ok") else "error")
        lines.extend(
            [
                f"### `{data.get('tool', 'unknown')}` — {semantic}",
                "",
                f"- Time: {event.get('timestamp', '')}",
                f"- Duration: {data.get('duration_ms', 0)} ms",
                "- Arguments:",
                "```json",
                json.dumps(data.get("arguments") or {}, ensure_ascii=False, indent=2),
                "```",
                "- Result:",
                "```text",
                str(data.get("result") or data.get("error") or ""),
                "```",
                "",
            ]
        )

    issues = session.get("analysis_issues") or []
    lines.extend(["## Analysis hints", ""])
    if not issues:
        lines.append("_No automatically detected issues._")
    else:
        for issue in issues:
            lines.append(f"- **{issue.get('type', 'issue')}**: {issue.get('message', '')}")
    lines.append("")
    return "\n".join(lines)


@dataclass
class ConversationSession:
    store: "ConversationLogStore"
    id: str
    directory: Path
    metadata: dict[str, Any]
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _sequence: int = 0
    _messages: list[dict[str, Any]] = field(default_factory=list)
    _finished: bool = False

    @property
    def metadata_path(self) -> Path:
        return self.directory / "session.json"

    @property
    def messages_path(self) -> Path:
        return self.directory / "messages.json"

    @property
    def events_path(self) -> Path:
        return self.directory / "events.jsonl"

    @property
    def markdown_path(self) -> Path:
        return self.directory / "transcript.md"

    def _save_metadata(self) -> None:
        _atomic_json(self.metadata_path, self.metadata)

    def append_event(self, event: str, data: Any | None = None) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            item = {
                "sequence": self._sequence,
                "timestamp": _utc_now(),
                "event": event,
                "data": _redact(data or {}),
            }
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            return item

    def update_runtime(self, **values: Any) -> None:
        with self._lock:
            for key, value in values.items():
                if value not in (None, ""):
                    self.metadata[key] = _redact(value, key)
            self._save_metadata()

    def observe_messages(self, messages: Any) -> None:
        normalised = _normalise_messages(messages)
        with self._lock:
            if normalised == self._messages:
                return
            previous = self._messages
            self._messages = normalised
            _atomic_json(self.messages_path, normalised)

            for index, message in enumerate(normalised):
                if index >= len(previous):
                    self.append_event("message", message)
                elif message != previous[index]:
                    self.append_event("message_update", message)

            dialogue = [
                item
                for item in normalised
                if item.get("role") in {"user", "assistant"} and item.get("content")
            ]
            self.metadata["message_count"] = len(dialogue)
            self.metadata["user_message_count"] = sum(item.get("role") == "user" for item in dialogue)
            self.metadata["assistant_message_count"] = sum(item.get("role") == "assistant" for item in dialogue)
            first_user = next((item.get("content", "") for item in dialogue if item.get("role") == "user"), "")
            last_user = next((item.get("content", "") for item in reversed(dialogue) if item.get("role") == "user"), "")
            self.metadata["first_user_message"] = first_user[:300]
            self.metadata["last_user_message"] = last_user[:300]
            self._save_metadata()
            self._write_markdown()

    def record_tool_call(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        ok: bool,
        duration_ms: int,
        result: str = "",
        error: str = "",
    ) -> None:
        lookup_miss, lookup_query = _is_lookup_miss(result, error)
        data = {
            "tool": tool,
            "arguments": _redact(arguments),
            "ok": bool(ok),
            "duration_ms": int(duration_ms),
            "result": str(result or "")[:50000],
            "error": str(error or "")[:10000],
            "lookup_miss": lookup_miss,
            "lookup_query": lookup_query,
        }
        self.append_event("mcp_tool_call", data)
        with self._lock:
            self.metadata["tool_call_count"] = int(self.metadata.get("tool_call_count") or 0) + 1
            if not ok:
                self.metadata["tool_error_count"] = int(self.metadata.get("tool_error_count") or 0) + 1
            if lookup_miss:
                self.metadata["lookup_miss_count"] = int(self.metadata.get("lookup_miss_count") or 0) + 1
                issues = self.metadata.setdefault("analysis_issues", [])
                message = f"Tool {tool} did not resolve"
                if lookup_query:
                    message += f" query “{lookup_query}”"
                message += ". Check the entity name/aliases and the subsequent fallback search."
                issues.append(
                    {
                        "type": "entity_lookup_miss",
                        "timestamp": _utc_now(),
                        "tool": tool,
                        "query": lookup_query,
                        "message": message,
                    }
                )
            self._save_metadata()
            self._write_markdown()

    def record_error(self, message: str, *, stage: str = "runtime") -> None:
        self.append_event("error", {"stage": stage, "message": str(message)})
        with self._lock:
            self.metadata["error_count"] = int(self.metadata.get("error_count") or 0) + 1
            self.metadata["last_error"] = str(message)[:2000]
            self._save_metadata()

    def record_client_event(self, event: str, data: Any | None = None) -> None:
        self.append_event(event, data)

    async def watch_context(self, context_getter: Callable[[], Any], interval: float = 0.75) -> None:
        """Persist the latest LLM context while the live session runs."""

        try:
            while not self._finished:
                context = context_getter()
                getter = getattr(context, "get_messages", None) if context is not None else None
                if callable(getter):
                    try:
                        self.observe_messages(getter() or [])
                    except Exception as err:
                        logger.debug("Conversation log context snapshot failed for {}: {}", self.id, err)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise

    def _read_events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        result: list[dict[str, Any]] = []
        for line in self.events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                result.append(item)
        return result

    def _write_markdown(self) -> None:
        try:
            events = self._read_events()
            metadata = dict(self.metadata)
            metadata["analysis_issues"] = _derive_analysis_issues(metadata, events)
            self.markdown_path.write_text(
                _markdown(metadata, self._messages, events),
                encoding="utf-8",
            )
        except Exception as err:
            logger.debug("Could not write conversation markdown {}: {}", self.id, err)

    def finish(self, *, status: str = "completed", error: str = "", messages: Any = None) -> None:
        with self._lock:
            if self._finished:
                return
            if messages is not None:
                self.observe_messages(messages)
            if error:
                self.record_error(error, stage="session")
            self._finished = True
            self.metadata["status"] = status
            self.metadata["finished_at"] = _utc_now()
            self.metadata["duration_seconds"] = round(
                max(0.0, time.time() - float(self.metadata.get("started_epoch") or time.time())),
                3,
            )
            self.append_event("session_finished", {"status": status, "error": error})
            self._save_metadata()
            self._write_markdown()


class ConversationLogStore:
    """Persistent store of per-session dialogue, tool calls, and analysis hints."""

    def __init__(self, root: Path = LOG_ROOT) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, ConversationSession] = {}
        self._lock = threading.RLock()

    def start_session(
        self,
        *,
        client_id: str,
        source: str,
        flow_id: str,
        flow_name: str,
        provider: str,
        model: str,
        language: str,
    ) -> ConversationSession:
        now = datetime.now(timezone.utc)
        session_id = f"{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:10]}"
        directory = self.root / _safe_component(session_id)
        metadata = {
            "id": session_id,
            "title": f"{flow_name} — {now.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "started_at": now.isoformat(),
            "started_epoch": time.time(),
            "finished_at": None,
            "status": "running",
            "client_id": client_id,
            "source": source,
            "flow_id": flow_id,
            "flow_name": flow_name,
            "provider": provider,
            "model": model,
            "language": language,
            "message_count": 0,
            "user_message_count": 0,
            "assistant_message_count": 0,
            "tool_call_count": 0,
            "tool_error_count": 0,
            "lookup_miss_count": 0,
            "error_count": 0,
            "analysis_issues": [],
        }
        session = ConversationSession(self, session_id, directory, metadata)
        directory.mkdir(parents=True, exist_ok=True)
        session._save_metadata()
        session.append_event("session_started", metadata)
        session._write_markdown()
        with self._lock:
            self._sessions[session_id] = session
        logger.info("Conversation log session started: {}", session_id)
        return session

    def get_active(self, session_id: str) -> ConversationSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def record_tool_call(self, session_id: str, **values: Any) -> None:
        session = self.get_active(session_id)
        if session:
            session.record_tool_call(**values)

    def list_sessions(self, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        for path in self.root.glob("*/session.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                records.append(data)
        records.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
        total = len(records)
        page = records[max(0, offset) : max(0, offset) + max(1, min(limit, 500))]
        return {
            "sessions": page,
            "total": total,
            "offset": max(0, offset),
            "limit": max(1, min(limit, 500)),
            "has_more": max(0, offset) + len(page) < total,
            "storage_path": str(self.root),
        }

    def read_session(self, session_id: str) -> dict[str, Any]:
        directory = self.root / _safe_component(session_id)
        metadata_path = directory / "session.json"
        if not metadata_path.exists():
            raise FileNotFoundError(session_id)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        messages_path = directory / "messages.json"
        messages = json.loads(messages_path.read_text(encoding="utf-8")) if messages_path.exists() else []
        events: list[dict[str, Any]] = []
        events_path = directory / "events.jsonl"
        if events_path.exists():
            for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
        metadata = dict(metadata)
        metadata["analysis_issues"] = _derive_analysis_issues(metadata, events)
        return {
            "session": metadata,
            "messages": messages,
            "events": events,
            "files": {
                "json": f"api/assist/conversations/{session_id}",
                "markdown": f"api/assist/conversations/{session_id}/markdown",
            },
        }

    def markdown(self, session_id: str) -> str:
        detail = self.read_session(session_id)
        return _markdown(detail["session"], detail["messages"], detail["events"])

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            active = self._sessions.pop(session_id, None)
            if active:
                active._finished = True
        directory = self.root / _safe_component(session_id)
        if not directory.exists():
            raise FileNotFoundError(session_id)
        shutil.rmtree(directory)

    def clear(self) -> int:
        count = 0
        for child in list(self.root.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
                count += 1
        with self._lock:
            self._sessions.clear()
        return count


CONVERSATION_LOGS = ConversationLogStore()


def conversation_logs_html() -> str:
    """Standalone, dependency-free browser for persisted conversation sessions."""

    return r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pipecat Conversation Logs</title>
<style>
:root{color-scheme:dark;--bg:#07111f;--panel:#0d1b2d;--line:#233852;--text:#eff7ff;--muted:#91a7bf;--accent:#56a5ff;--bad:#ff7474;--good:#70d69b;--warn:#ffc76b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}button,a{font:inherit}.top{position:sticky;top:0;z-index:5;display:flex;gap:12px;align-items:center;padding:14px 18px;background:#091728;border-bottom:1px solid var(--line)}.top h1{font-size:18px;margin:0}.top a{color:var(--accent);text-decoration:none}.grid{display:grid;grid-template-columns:minmax(280px,380px) 1fr;min-height:calc(100vh - 57px)}.sessions{border-right:1px solid var(--line);padding:12px;overflow:auto}.detail{padding:18px;overflow:auto}.session{width:100%;text-align:left;padding:12px;margin-bottom:8px;border:1px solid var(--line);border-radius:12px;background:var(--panel);color:var(--text);cursor:pointer}.session:hover,.session.active{border-color:var(--accent)}.session strong,.session span{display:block}.session small,.muted{color:var(--muted)}.bad{color:var(--bad)}.good{color:var(--good)}.warn{color:var(--warn)}.pill{display:inline-block;padding:2px 7px;border:1px solid var(--line);border-radius:999px;margin:3px 4px 0 0;font-size:12px}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 18px}.toolbar button,.toolbar a{border:1px solid var(--line);border-radius:9px;padding:7px 10px;background:var(--panel);color:var(--text);text-decoration:none;cursor:pointer}.timeline{display:flex;flex-direction:column;gap:10px}.event{border:1px solid var(--line);border-radius:12px;padding:12px;background:var(--panel)}.event.user{border-left:4px solid var(--accent)}.event.assistant{border-left:4px solid var(--good)}.event.tool{border-left:4px solid var(--warn)}.event.error{border-left:4px solid var(--bad)}.event h3{font-size:14px;margin:0 0 8px}.event pre{white-space:pre-wrap;word-break:break-word;background:#07111f;border-radius:8px;padding:10px;max-height:360px;overflow:auto}.issues{border:1px solid #765b25;background:#211b0d;border-radius:12px;padding:12px;margin:12px 0}.empty{color:var(--muted);padding:32px;text-align:center}@media(max-width:820px){.grid{display:block}.sessions{border-right:0;border-bottom:1px solid var(--line);max-height:42vh}.detail{padding:12px}}
</style>
</head>
<body>
<header class="top"><a href="./">← Pipecat Assist</a><h1>Conversation Logs</h1><span id="count" class="muted"></span><button onclick="loadSessions()">Обновить</button></header>
<div class="grid"><aside id="sessions" class="sessions"></aside><main id="detail" class="detail"><div class="empty">Выберите диалог слева</div></main></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let selected='';
async function loadSessions(){const r=await fetch('api/assist/conversations?limit=200',{cache:'no-store'});const d=await r.json();document.getElementById('count').textContent=`${d.total} сессий`;const box=document.getElementById('sessions');box.innerHTML=d.sessions.map(s=>`<button class="session ${selected===s.id?'active':''}" onclick="loadSession('${esc(s.id)}')"><strong>${esc(s.first_user_message||s.flow_name||s.id)}</strong><small>${esc(s.started_at)} · ${esc(s.status)}</small><span><i class="pill">реплик ${s.message_count||0}</i><i class="pill">tools ${s.tool_call_count||0}</i>${s.lookup_miss_count?`<i class="pill warn">не найдено ${s.lookup_miss_count}</i>`:''}${s.error_count?`<i class="pill bad">ошибок ${s.error_count}</i>`:''}</span></button>`).join('')||'<div class="empty">Логов пока нет</div>'}
function renderMessage(m,ts=''){const role=m.role==='user'?'user':m.role==='assistant'?'assistant':'';if(!role||!m.content)return'';return `<section class="event ${role}"><h3>${role==='user'?'Пользователь':'Помощник'}</h3>${ts?`<small>${esc(ts)}</small>`:''}<div>${esc(m.content).replace(/\n/g,'<br>')}</div></section>`}
function renderTool(e){const x=e.data||{}, cls=x.lookup_miss?'warn':x.ok?'good':'bad';return `<section class="event tool"><h3>Инструмент: <code>${esc(x.tool)}</code> <span class="${cls}">${x.lookup_miss?'не найдено':x.ok?'успешно':'ошибка'}</span></h3><small>${esc(e.timestamp)} · ${x.duration_ms||0} ms</small><details open><summary>Аргументы</summary><pre>${esc(JSON.stringify(x.arguments||{},null,2))}</pre></details><details><summary>Результат</summary><pre>${esc(x.result||x.error||'')}</pre></details></section>`}
function chronological(d){const firstTimes={};for(const e of d.events||[]){if((e.event==='message'||e.event==='message_update')&&e.data&&firstTimes[e.data.index]===undefined)firstTimes[e.data.index]=e.timestamp}const items=[];for(const m of d.messages||[]){if((m.role==='user'||m.role==='assistant')&&m.content)items.push({time:firstTimes[m.index]||'',html:renderMessage(m,firstTimes[m.index]||'')})}for(const e of d.events||[]){if(e.event==='mcp_tool_call')items.push({time:e.timestamp||'',html:renderTool(e)});if(e.event==='error')items.push({time:e.timestamp||'',html:`<section class="event error"><h3>Ошибка</h3><small>${esc(e.timestamp||'')}</small><pre>${esc(JSON.stringify(e.data||{},null,2))}</pre></section>`})}items.sort((a,b)=>String(a.time).localeCompare(String(b.time)));return items.map(x=>x.html).join('')}
async function loadSession(id){selected=id;await loadSessions();const r=await fetch(`api/assist/conversations/${encodeURIComponent(id)}`,{cache:'no-store'});const d=await r.json(),s=d.session, detail=document.getElementById('detail');const issues=(s.analysis_issues||[]).map(i=>`<li>${esc(i.message)}</li>`).join('');const timeline=chronological(d);detail.innerHTML=`<h2>${esc(s.first_user_message||s.flow_name||s.id)}</h2><div class="muted">${esc(s.started_at)} → ${esc(s.finished_at||'идёт сейчас')} · ${esc(s.provider)}/${esc(s.model)}</div><div class="toolbar"><a href="api/assist/conversations/${encodeURIComponent(id)}/markdown" target="_blank">Markdown</a><a href="api/assist/conversations/${encodeURIComponent(id)}" target="_blank">JSON</a><button onclick="removeSession('${esc(id)}')">Удалить</button></div>${issues?`<div class="issues"><strong>Подсказки анализа</strong><ul>${issues}</ul></div>`:''}<h2>Хронология</h2><div class="timeline">${timeline||'<div class="empty">Событий пока нет</div>'}</div>`}
async function removeSession(id){if(!confirm('Удалить этот лог?'))return;await fetch(`api/assist/conversations/${encodeURIComponent(id)}`,{method:'DELETE'});selected='';document.getElementById('detail').innerHTML='<div class="empty">Лог удалён</div>';loadSessions()}
loadSessions();setInterval(()=>{if(selected)loadSession(selected);else loadSessions()},15000);
</script>
</body></html>'''

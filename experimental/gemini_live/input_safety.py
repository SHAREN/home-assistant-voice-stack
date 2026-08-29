"""Deterministic input and high-risk tool safety for Gemini Live."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
import time
import unicodedata
from typing import Any


LOCAL_STOP_SENTINEL = "__gemini_live_local_stop__"
OFFLINE_INPUT_SENTINEL = "__gemini_live_offline_network__"

_RUSSIAN_ALPHABET = frozenset("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")
_LATIN_ALLOWLIST = frozenset(
    {
        "android", "apple", "assistant", "bluetooth", "gemini", "google",
        "hdmi", "home", "honda", "jazz", "jbl", "netflix", "p610",
        "spotify", "tv", "wifi", "youtube",
    }
)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")

_STOP_PHRASES = (
    ("до", "свидания"),
    ("закончили",),
    ("стоп",),
    ("все",),
    ("хватит",),
)
_STOP_FILLERS = frozenset({"пожалуйста"})

ENGINE_ENTITY_ID = "switch.honda_jazz_engine"
ENGINE_CONFIRMATION_PROMPT = (
    "Для запуска двигателя скажите: «подтверждаю запуск двигателя»."
)
RUSSIAN_ONLY_TOOL_INSTRUCTION = (
    "Принимай и интерпретируй пользовательские команды только на русском языке. "
    "Отдельные латинские названия брендов и технологий внутри русской фразы допустимы. "
    "Если вход не русский, непонятный или сомнительный, не вызывай никакие инструменты "
    "и попроси повторить по-русски. Слова «стоп», «всё», «хватит», «закончили» и "
    "«до свидания» означают завершение разговора и не являются командами устройствам."
)
FOREIGN_READ_ONLY_INSTRUCTION = (
    "Распознавание этой реплики, возможно, ошибочно. Не выполняй никаких команд "
    "или действий. Коротко попроси пользователя повторить или уточнить реплику."
)
_ENGINE_START_PATTERNS = (
    re.compile(r"^(?:пожалуйста )?(?:заведи|завести) (?:машину|автомобиль|хонду|honda jazz)$"),
    re.compile(
        r"^(?:пожалуйста )?(?:запусти|запустить|включи|включить) "
        r"(?:двигатель|мотор)(?: (?:машины|автомобиля|honda jazz|хонды))?$"
    ),
)
_ENGINE_CONFIRMATIONS = frozenset(
    {
        "подтверждаю запуск двигателя",
        "да подтверждаю запуск двигателя",
    }
)
_ENGINE_OFF_PATTERNS = (
    re.compile(r"^(?:пожалуйста )?(?:заглуши|заглушить) (?:машину|автомобиль|хонду|honda jazz)$"),
    re.compile(
        r"^(?:пожалуйста )?(?:выключи|выключить|останови|остановить) "
        r"(?:двигатель|мотор)(?: (?:машины|автомобиля|honda jazz|хонды))?$"
    ),
)
_ENGINE_CONFIRMATION_TTL_SECONDS = 30.0


class InputAction(StrEnum):
    """Local action selected before any tool-capable model call."""

    ACCEPT = "accept"
    REJECT_FOREIGN = "reject_foreign"
    LOCAL_STOP = "local_stop"
    OFFLINE_NETWORK = "offline_network"


@dataclass(frozen=True, slots=True)
class InputSafetyDecision:
    """Privacy-safe deterministic classification result."""

    action: InputAction
    reason: str
    cyrillic_letters: int
    latin_letters: int
    other_letters: int


class EngineLocalAction(StrEnum):
    """High-risk engine action selected before Gemini receives the text."""

    NONE = "none"
    REQUEST_CONFIRMATION = "request_confirmation"
    CONFIRMED_ON = "confirmed_on"
    OFF = "off"
    CANCELLED = "cancelled"


def normalize_utterance(text: str) -> str:
    """Normalize case, punctuation and spacing without fuzzy matching."""
    normalized = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
    words = _WORD_RE.findall(normalized)
    return _SPACE_RE.sub(" ", " ".join(words)).strip()


def _is_local_stop(normalized: str) -> bool:
    """Return whether the whole utterance consists only of stop phrases."""
    tokens = tuple(token for token in normalized.split() if token not in _STOP_FILLERS)
    if not tokens:
        return False
    reachable = {0}
    for index in range(len(tokens)):
        if index not in reachable:
            continue
        for phrase in _STOP_PHRASES:
            if tokens[index : index + len(phrase)] == phrase:
                reachable.add(index + len(phrase))
    return len(tokens) in reachable


def is_local_stop_phrase(text: str) -> bool:
    """Recognize an exact local stop without applying the language authority gate."""
    return _is_local_stop(normalize_utterance(text))


def classify_russian_input(text: str) -> InputSafetyDecision:
    """Accept Russian-dominant input, or reject it before tools are available."""
    normalized = normalize_utterance(text)
    if _is_local_stop(normalized):
        return InputSafetyDecision(InputAction.LOCAL_STOP, "local_stop_phrase", 0, 0, 0)

    cyrillic = 0
    latin = 0
    other = 0
    non_russian_cyrillic = 0
    for char in unicodedata.normalize("NFKC", text or "").casefold():
        if not char.isalpha():
            continue
        if "CYRILLIC" in unicodedata.name(char, ""):
            cyrillic += 1
            if char not in _RUSSIAN_ALPHABET:
                non_russian_cyrillic += 1
        elif "LATIN" in unicodedata.name(char, ""):
            latin += 1
        else:
            other += 1

    if non_russian_cyrillic:
        reason = "non_russian_cyrillic"
    elif cyrillic < 2:
        reason = "insufficient_russian_script"
    elif other:
        reason = "unsupported_letter_script"
    else:
        total = cyrillic + latin
        ratio = cyrillic / total if total else 0.0
        latin_tokens = {
            token.casefold()
            for token in _WORD_RE.findall(unicodedata.normalize("NFKC", text or ""))
            if any("LATIN" in unicodedata.name(char, "") for char in token if char.isalpha())
        }
        latin_is_allowlisted = bool(latin_tokens) and latin_tokens <= _LATIN_ALLOWLIST
        if ratio >= 0.55 or not latin or latin_is_allowlisted:
            return InputSafetyDecision(
                InputAction.ACCEPT,
                "russian_script_accepted",
                cyrillic,
                latin,
                other,
            )
        reason = "cyrillic_ratio_below_threshold"

    return InputSafetyDecision(
        InputAction.REJECT_FOREIGN,
        reason,
        cyrillic,
        latin,
        other,
    )


def speech_result_text(text: str, decision: InputSafetyDecision) -> str:
    """Hide exact stop text while preserving read-only foreign transcripts."""
    if decision.action is InputAction.LOCAL_STOP:
        return LOCAL_STOP_SENTINEL
    return text.strip()


def _flatten_text_values(value: Any) -> list[str]:
    """Flatten tool arguments for a conservative engine-target check."""
    if isinstance(value, str):
        return [normalize_utterance(value.replace("_", " ").replace(".", " "))]
    if isinstance(value, dict):
        return [
            item
            for nested in value.values()
            for item in _flatten_text_values(nested)
        ]
    if isinstance(value, (list, tuple, set)):
        return [
            item
            for nested in value
            for item in _flatten_text_values(nested)
        ]
    return []


def is_engine_on_tool_call(tool_name: str, tool_args: Any) -> bool:
    """Block stale Assist exposure from turning the engine on."""
    compact_name = normalize_utterance(tool_name.replace("_", " ")).replace(" ", "")
    raw_target_text = " ".join(_flatten_text_values(tool_args)).casefold()
    target_text = normalize_utterance(raw_target_text.replace("_", " "))
    targets_engine = any(
        marker in target_text
        for marker in (
            "switch honda jazz engine",
            "honda jazz engine",
            "honda jazz",
            "хонда джаз",
            "машина двигатель",
            "двигатель машины",
        )
    )
    if not targets_engine:
        return False
    compact_args = target_text.replace(" ", "")
    raw_compact_args = raw_target_text.replace(" ", "")
    return any(
        marker in compact_name or marker in compact_args or marker in raw_compact_args
        for marker in (
            "turnon",
            "turn_on",
            "switchon",
            "stateon",
            "включ",
            "запуст",
            "завед",
        )
    )


def is_explicit_engine_start_request(text: str) -> bool:
    """Require an anchored affirmative Russian engine-start command."""
    normalized = normalize_utterance(text)
    return any(pattern.fullmatch(normalized) for pattern in _ENGINE_START_PATTERNS)


def is_explicit_engine_off_request(text: str) -> bool:
    """Recognize an anchored Russian engine-stop command."""
    normalized = normalize_utterance(text)
    return any(pattern.fullmatch(normalized) for pattern in _ENGINE_OFF_PATTERNS)


def is_engine_confirmation(text: str) -> bool:
    """Recognize only the documented explicit confirmation sentence."""
    return normalize_utterance(text) in _ENGINE_CONFIRMATIONS


class EngineSafetyGuard:
    """Keep bounded, per-conversation two-turn engine confirmation state."""

    def __init__(self, *, monotonic: Any = time.monotonic) -> None:
        self._monotonic = monotonic
        self._pending_until: dict[str, float] = {}

    def classify_turn(self, conversation_id: str, text: str) -> EngineLocalAction:
        """Select an engine action; every non-confirming next turn clears pending."""
        for pending_id in tuple(self._pending_until):
            if pending_id != conversation_id:
                self._pending_until.pop(pending_id, None)
        deadline = self._pending_until.get(conversation_id)
        if deadline is not None:
            self._pending_until.pop(conversation_id, None)
            if self._monotonic() <= deadline and is_engine_confirmation(text):
                return EngineLocalAction.CONFIRMED_ON
            if is_explicit_engine_off_request(text):
                return EngineLocalAction.OFF
            return EngineLocalAction.CANCELLED

        if is_explicit_engine_start_request(text):
            self._pending_until[conversation_id] = (
                self._monotonic() + _ENGINE_CONFIRMATION_TTL_SECONDS
            )
            return EngineLocalAction.REQUEST_CONFIRMATION
        if is_explicit_engine_off_request(text):
            return EngineLocalAction.OFF
        return EngineLocalAction.NONE

    def clear(self, conversation_id: str) -> None:
        """Discard pending confirmation when the conversation ends/rejects."""
        self._pending_until.pop(conversation_id, None)

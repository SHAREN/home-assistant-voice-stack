"""Deterministic safety guard for voice-triggered Home Assistant side effects."""

from __future__ import annotations

import asyncio
import inspect
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str = ""


# Canonical Home Assistant areas and the spoken forms used in this home.
# "Комната" is intentionally special: there is currently no HA area with that
# exact name, so it must never silently fall back to the default Hall.
_ROOM_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Прихожая", ("прихож", "холл", "у вход")),
    ("Коридор", ("коридор", "проход")),
    ("Туалет", ("туалет",)),
    ("Ванная", ("ванн",)),
    ("Кухня", ("кухн",)),
    ("Спальня", ("спальн",)),
    ("Гостиная", ("гостин",)),
    ("Зал", ("зал",)),
    ("Комната", ("комнат",)),
)

_ACTION_WORDS = {
    "включи", "включите", "включить", "выключи", "выключите", "выключить",
    "сделай", "сделайте", "сделать", "поставь", "поставьте", "поставить",
    "установи", "установите", "установить", "задай", "задайте", "задать",
    "добавь", "добавьте", "добавить", "убавь", "убавьте", "убавить",
    "прибавь", "прибавьте", "прибавить", "увеличь", "увеличьте", "увеличить",
    "уменьши", "уменьшите", "уменьшить", "запусти", "запустите", "запустить",
    "останови", "остановите", "остановить", "отмени", "отмените", "отменить",
    "продолжи", "продолжите", "продолжить", "верни", "верните", "вернуть",
    "убери", "уберите", "убрать", "пропылесось", "пропылесосьте",
    "пропылесосить", "очисти", "очистите", "очистить", "закрой", "закройте",
    "закрыть", "открой", "откройте", "открыть", "удали", "удалите", "удалить",
    "отметь", "отметьте", "отметить", "заверши", "завершите", "завершить",
    "громче", "тише", "ярче", "темнее", "следующий", "следующую",
    "предыдущий", "предыдущую", "пауза", "mute", "unmute", "play", "pause",
}

_POWER_ON_WORDS = {
    "включи", "включите", "включить", "запусти", "запустите", "запустить",
    "открой", "откройте", "открыть", "активируй", "активируйте", "активировать",
}
_POWER_OFF_WORDS = {
    "выключи", "выключите", "выключить", "отключи", "отключите", "отключить",
    "останови", "остановите", "остановить", "закрой", "закройте", "закрыть",
    "деактивируй", "деактивируйте", "деактивировать",
}


_LIGHT_STEMS = ("свет", "ламп", "освещ")

_READ_ONLY_SHORT_NAMES = {
    "GetLiveContext",
    "GetDateTime",
    "todo_get_items",
    "ha_get_overview",
    "ha_get_skill_guide",
    "ha_search",
    "ha_search_tools",
    "ha_report_issue",
}

_IMMEDIATE_CONTROL_TOOLS = {"HassTurnOn", "HassTurnOff", "HassLightSet"}
_DEFERRED_ACTION_RE = re.compile(
    r"(?:^|\s)(?:через|когда|после\s+того\s+как|как\s+только)(?:\s|$)",
    re.IGNORECASE,
)


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return " ".join(text.split())


def _short_tool_name(name: str) -> str:
    return str(name or "").rsplit("__", 1)[-1]


def _is_mutating_tool(name: str) -> bool:
    short = _short_tool_name(name)
    if short in _READ_ONLY_SHORT_NAMES:
        return False
    if short.startswith(("ha_get_", "ha_list_", "ha_search")):
        return False
    return short.startswith(("Hass", "ha_config_set_", "ha_config_remove_", "ha_set_", "ha_remove_"))


def _has_action_intent(text: str) -> bool:
    tokens = _normalize(text).split()
    return bool(tokens) and any(token in _ACTION_WORDS for token in tokens)


def _requested_power_action(text: str) -> str | None:
    """Return the last explicit on/off command in the utterance.

    Exact tokens are intentional: past-tense/status words such as "выключил"
    or "выключен" must not be mistaken for commands.
    """

    requested: str | None = None
    for token in _normalize(text).split():
        if token in _POWER_ON_WORDS:
            requested = "on"
        elif token in _POWER_OFF_WORDS:
            requested = "off"
    return requested


def _has_deferred_action_modifier(text: str) -> bool:
    return _DEFERRED_ACTION_RE.search(_normalize(text)) is not None


def _contains_room_needle(normalized: str, needle: str) -> bool:
    """Match spoken room stems on token boundaries, never inside unrelated words."""

    needle_norm = _normalize(needle)
    if not needle_norm:
        return False
    if " " in needle_norm:
        return re.search(rf"(?<!\w){re.escape(needle_norm)}(?:\s|$)", normalized) is not None
    return any(token.startswith(needle_norm) for token in normalized.split())


def _explicit_room(text: str) -> str | None:
    normalized = _normalize(text)
    for canonical, needles in _ROOM_PATTERNS:
        if any(_contains_room_needle(normalized, needle) for needle in needles):
            return canonical
    return None


def _target_text(arguments: dict[str, Any]) -> str:
    bits: list[str] = []
    for key in ("area", "name", "floor"):
        value = arguments.get(key)
        if isinstance(value, list):
            bits.extend(str(item) for item in value)
        elif value is not None:
            bits.append(str(value))
    return _normalize(" ".join(bits))


def _area_matches(arguments: dict[str, Any], canonical: str) -> bool:
    area = _normalize(arguments.get("area"))
    canonical_norm = _normalize(canonical)
    if canonical == "Комната":
        # No canonical HA area exists yet. A specifically named entity/device
        # containing "комната" is acceptable, but falling back to Hall is not.
        target = _target_text(arguments)
        return "комнат" in target and "зал" not in area
    if area:
        return canonical_norm in area
    # A specific target name that itself carries the room is acceptable.
    target = _target_text(arguments)
    needles = next((items for room, items in _ROOM_PATTERNS if room == canonical), ())
    return any(needle in target for needle in needles)


def _looks_like_light_control(name: str, arguments: dict[str, Any], text: str) -> bool:
    short = _short_tool_name(name)
    if short not in {"HassTurnOn", "HassTurnOff", "HassLightSet"}:
        return False
    normalized = _normalize(text)
    if any(stem in normalized for stem in _LIGHT_STEMS):
        return True
    domains = arguments.get("domain")
    if isinstance(domains, str):
        domains = [domains]
    if isinstance(domains, list) and "light" in {str(item).casefold() for item in domains}:
        return True
    return "торшер" in _normalize(arguments.get("name"))


def evaluate_voice_control(
    *,
    source: str,
    tool_name: str,
    arguments: dict[str, Any],
    transcript: str,
) -> GuardDecision:
    """Fail closed for side effects that are not grounded in the current turn."""

    if not _is_mutating_tool(tool_name):
        return GuardDecision(True)

    text = str(transcript or "").strip()
    if not _has_action_intent(text):
        return GuardDecision(
            False,
            f"{source}: no explicit actionable intent in the current user turn",
        )

    short = _short_tool_name(tool_name)
    requested_power = _requested_power_action(text)
    if short == "HassTurnOn" and requested_power != "on":
        return GuardDecision(
            False,
            f"{source}: current user turn does not explicitly request turn-on",
        )
    if short == "HassTurnOff" and requested_power != "off":
        return GuardDecision(
            False,
            f"{source}: current user turn does not explicitly request turn-off",
        )

    if (
        short in _IMMEDIATE_CONTROL_TOOLS
        and _has_deferred_action_modifier(text)
    ):
        return GuardDecision(
            False,
            f"{source}: deferred/conditional wording requires non-immediate execution; do not run a direct control tool now",
        )

    # An explicitly spoken room always wins over defaults for every mutating
    # Home Assistant action. If the proposed tool target cannot prove that room,
    # fail closed instead of acting globally or in Hall.
    room = _explicit_room(text)
    if room and not _area_matches(arguments, room):
        return GuardDecision(
            False,
            f"{source}: explicit room {room!r} does not match tool target; never fall back to Hall/global scope",
        )

    if not _looks_like_light_control(tool_name, arguments, text):
        return GuardDecision(True)

    normalized = _normalize(text)
    generic_light_wording = any(stem in normalized for stem in _LIGHT_STEMS)
    area = _normalize(arguments.get("area"))
    name = _normalize(arguments.get("name"))

    if room == "Зал" and generic_light_wording:
        if "зал" not in area or "торшер" not in name:
            return GuardDecision(
                False,
                f"{source}: generic light in Hall must target name='Торшеры', area='Зал'",
            )
        return GuardDecision(True)

    if room:
        return GuardDecision(True)

    # Alice-like default requested by the user: a roomless light action may not
    # drift into another room. Generic "light" means exactly Torsery; a named
    # fixture is allowed, but still only in Hall.
    if "зал" not in area:
        return GuardDecision(
            False,
            f"{source}: roomless light action must stay in area='Зал'",
        )
    if generic_light_wording and "торшер" not in name:
        return GuardDecision(
            False,
            f"{source}: roomless generic light command must target name='Торшеры', area='Зал'",
        )

    return GuardDecision(True)


class VoiceControlGuard:
    """Debounce mutating voice tools until the user has finished the utterance."""

    def __init__(
        self,
        transcript_getter: Callable[[], Any],
        *,
        source: str,
        settle_seconds: float = 0.30,
        activity_getter: Callable[[], Any] | None = None,
        quiet_seconds: float = 0.0,
    ) -> None:
        self.transcript_getter = transcript_getter
        self.source = source
        self.settle_seconds = max(0.0, float(settle_seconds))
        self.activity_getter = activity_getter
        self.quiet_seconds = max(0.0, float(quiet_seconds))

    async def _read_getter(self, getter: Callable[[], Any]) -> Any:
        value = getter()
        if inspect.isawaitable(value):
            value = await value
        return value

    async def __call__(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        if not _is_mutating_tool(tool_name):
            return None

        # Gemini Live can propose a function before the user's final transcript is
        # committed to LLMContext. Treat the whole settle_seconds value as one total
        # deadline (including the speech-quiet debounce) and require a fresh transcript
        # if microphone activity continued after the proposal. This prevents both:
        #   * executing from a stale previous turn, and
        #   * cancelling a valid command merely because Gemini proposed it mid-sentence.
        loop = asyncio.get_running_loop()
        proposed_at = loop.time()
        deadline = proposed_at + self.settle_seconds
        initial_value = str(await self._read_getter(self.transcript_getter) or "")
        initial_norm = _normalize(initial_value)
        continued_after_proposal = False

        if self.activity_getter is not None and self.quiet_seconds > 0:
            while loop.time() < deadline:
                raw_activity = await self._read_getter(self.activity_getter)
                try:
                    last_activity = float(raw_activity or 0.0)
                except (TypeError, ValueError):
                    last_activity = 0.0
                if last_activity > proposed_at + 0.01:
                    continued_after_proposal = True
                if last_activity <= 0:
                    break
                remaining_quiet = self.quiet_seconds - (loop.time() - last_activity)
                if remaining_quiet <= 0:
                    break
                await asyncio.sleep(min(0.05, remaining_quiet, max(0.0, deadline - loop.time())))

        last_reason = f"{self.source}: final current-turn transcript did not arrive before control deadline"
        while loop.time() < deadline:
            value = str(await self._read_getter(self.transcript_getter) or "")
            current_norm = _normalize(value)

            # If the user kept speaking after the function proposal, the transcript
            # visible at proposal time belongs to an incomplete/stale turn. Never use
            # it to authorize a side effect; wait until transcription advances.
            if continued_after_proposal and current_norm == initial_norm:
                await asyncio.sleep(min(0.10, max(0.0, deadline - loop.time())))
                continue

            decision = evaluate_voice_control(
                source=self.source,
                tool_name=tool_name,
                arguments=dict(arguments or {}),
                transcript=value,
            )
            if decision.allowed:
                return None
            last_reason = decision.reason

            # Once a fresh transcript exists, deterministic target/action mismatches
            # are real and should fail closed immediately. Missing intent can still
            # mean the final STT is arriving in chunks, so keep polling it.
            if current_norm != initial_norm and "no explicit actionable intent" not in decision.reason:
                return decision.reason
            await asyncio.sleep(min(0.10, max(0.0, deadline - loop.time())))

        if continued_after_proposal:
            return (
                f"{self.source}: pending control superseded because the final current-turn "
                "transcript did not stabilize before the control deadline; do not report failure, "
                "re-evaluate the complete current user turn and issue exactly one corrected control tool"
            )
        return last_reason

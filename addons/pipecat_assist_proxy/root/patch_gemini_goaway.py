from pathlib import Path
import os

path = Path(os.environ.get(
    "PIPECAT_GEMINI_LLM_PATH",
    "/usr/local/lib/python3.11/dist-packages/pipecat/services/google/gemini_live/llm.py",
))
text = path.read_text()

anchor1 = '''        # Session resumption
        self._session_resumption_handle: str | None = None
'''
replacement1 = '''        # Session resumption
        self._session_resumption_handle: str | None = None
        # Google sends GoAway before a Live WebSocket rotation. Keep the old
        # connection alive through an active turn, then reconnect with the
        # latest session-resumption handle instead of waiting for ABORTED.
        self._go_away_pending = False
'''
if replacement1 not in text:
    if text.count(anchor1) != 1:
        raise SystemExit(f"Unexpected GoAway init anchor count: {text.count(anchor1)}")
    text = text.replace(anchor1, replacement1, 1)

anchor2 = '''                        if message.session_resumption_update:
                            self._handle_msg_resumption_update(message)
'''
replacement2 = '''                        if message.session_resumption_update:
                            self._handle_msg_resumption_update(message)

                        # Managed Live connection rotation. Google sends GoAway
                        # before the physical WebSocket expires. Do not tear down
                        # an in-flight user/model turn; rotate as soon as the
                        # service is idle, preserving context via the latest
                        # session-resumption handle.
                        go_away = getattr(message, "go_away", None)
                        if go_away:
                            self._go_away_pending = True
                            logger.info(
                                "Gemini GoAway received; managed session-resumption "
                                f"rotation pending, time_left={getattr(go_away, 'time_left', None)}"
                            )
                        if (
                            self._go_away_pending
                            and not self._user_is_speaking
                            and not self._bot_is_responding
                        ):
                            logger.info(
                                "Gemini managed GoAway rotation starting with "
                                "session resumption"
                            )
                            self._go_away_pending = False
                            await self._reconnect()
                            return
'''
if replacement2 not in text:
    if text.count(anchor2) != 1:
        raise SystemExit(f"Unexpected GoAway receive anchor count: {text.count(anchor2)}")
    text = text.replace(anchor2, replacement2, 1)

anchor3 = '''    async def _handle_session_ready(self, session: AsyncSession):
        """Handle the session being ready."""
        self._session = session
'''
replacement3 = '''    async def _handle_session_ready(self, session: AsyncSession):
        """Handle the session being ready."""
        self._go_away_pending = False
        self._session = session
'''
if replacement3 not in text:
    if text.count(anchor3) != 1:
        raise SystemExit(f"Unexpected GoAway ready anchor count: {text.count(anchor3)}")
    text = text.replace(anchor3, replacement3, 1)

path.write_text(text)
print("Patched Gemini Live managed GoAway/session-resumption rotation")

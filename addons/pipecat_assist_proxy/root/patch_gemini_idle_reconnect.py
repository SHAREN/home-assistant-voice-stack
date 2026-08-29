from pathlib import Path

path = Path('/usr/local/lib/python3.11/dist-packages/pipecat/services/google/gemini_live/llm.py')
text = path.read_text()
old = '''    async def _handle_connection_error(self, error: Exception) -> bool:\n        \"\"\"Handle a connection error and determine if reconnection should be attempted.\n\n        Args:\n            error: The exception that caused the connection error.\n\n        Returns:\n            True if reconnection should be attempted, False if a fatal error should be pushed.\n        \"\"\"\n        self._consecutive_failures += 1\n'''
new = '''    async def _handle_connection_error(self, error: Exception) -> bool:\n        \"\"\"Handle a connection error and determine if reconnection should be attempted.\n\n        Args:\n            error: The exception that caused the connection error.\n\n        Returns:\n            True if reconnection should be attempted, False if a fatal error should be pushed.\n        \"\"\"\n        # A connection that lived beyond CONNECTION_ESTABLISHED_THRESHOLD was\n        # healthy even if Gemini sent no application messages during that time.\n        # The upstream implementation only resets this counter while consuming a\n        # server message. In warm/idle voice standby there may be no messages for\n        # minutes, so periodic Gemini 3.1 server-side ABORTED/1008 resets were\n        # incorrectly accumulated as 1/3, 2/3, 3/3 and escalated to a fatal P610\n        # failure. Reset by elapsed connection lifetime before counting the new\n        # error, so an isolated server rotation stays a normal reconnect.\n        self._check_and_reset_failure_counter()\n        self._consecutive_failures += 1\n'''
count = text.count(old)
if count != 1:
    raise SystemExit(f'Unexpected Gemini error-handler patch target count: {count}')
path.write_text(text.replace(old, new, 1))
print('Patched Gemini Live idle reconnect failure-counter handling')

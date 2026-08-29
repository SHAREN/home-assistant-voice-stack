# End-to-end WebRTC test

Тест подключается к Pipecat runner как настоящий WebRTC-клиент, ждёт окончания стартового приветствия, передаёт русскую голосовую фразу и проверяет наличие непрерывного аудиоответа Gemini.

## Подготовка

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r tests/requirements.txt
```

Подготовьте короткий аудиофайл с русской командой. Сам аудиофайл не хранится в Git.

## Запуск

```bash
PIPECAT_E2E_PROMPT=/path/to/russian_prompt.mp3 \
python tests/e2e_webrtc_test.py
```

Переменные:

- `PIPECAT_HA_HOST` — адрес Home Assistant, по умолчанию `homeassistant.local`.
- `PIPECAT_STATUS_URL` — полный URL `/api/assist/status`, если стандартный порт runner отличается.
- `PIPECAT_E2E_PROMPT` — входной файл с русской речью.
- `PIPECAT_E2E_OUTPUT` — выходной WAV с ответом; по умолчанию `tests/e2e_response.wav`.

Успешный запуск завершается кодом `0` и JSON-полем `"ok": true`.

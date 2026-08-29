# Local development governance: Home Assistant voice stack

These rules are mandatory whenever work concerns Pipecat, Pipecat Assist, the Pipecat Live Lovelace card, Gemini routing for the voice stack, or the project documentation.

## Source of truth

The authoritative Git repository is on the user's **@home-PC** connector:

`D:\codexpro_workspace\home-assistant-voice-stack`

Home Assistant contains deployed runtime copies only. Do not treat files under `/addons` or `/config` as the development source of truth.

## Required workflow for every change

1. Before modifying the voice stack, open the repository through **@home-PC**, inspect `git status`, and read:
   - `docs/PROJECT_GOAL.md`
   - `docs/CURRENT_STATE.md`
   - `docs/OPERATING_RULES.md`
   - the latest entries in `docs/ACTIVITY_LOG.md` and `docs/CHANGELOG.md`.
2. Make the change in the Home PC repository first. If an emergency runtime fix must be made directly on Home Assistant, copy the exact final change back into the repository before declaring the task complete.
3. Run the relevant syntax checks and tests. For voice-path changes, run the WebRTC end-to-end test when possible.
4. Deploy the tested files to Home Assistant and verify the actual runtime state and logs.
5. Update human-readable documentation:
   - `docs/CURRENT_STATE.md` for the resulting live state;
   - `docs/CHANGELOG.md` and `docs/ACTIVITY_LOG.md` for what changed and why;
   - `docs/DECISIONS.md` for architectural or policy decisions;
   - `docs/PROJECT_GOAL.md` only when the project goal changes.
6. Create a Git commit for every completed logical change. Use a meaningful message that states the result, not a vague message such as “fix” or “update”. Record the commit hash in the activity log/current state when it changes the deployed system.
7. Sync the runtime-readable documentation to `/config/voice_stack_docs/` on Home Assistant.

## Safety and exclusions

Never commit or copy into project memory API keys, access tokens, SSH keys, passwords, `.env`, Home Assistant `.storage`, databases, conversation transcripts, raw voice recordings, cookies, or credentials. Technical logs may be summarized, but secrets and private user speech must not be stored in Git.

Do not leave an uncommitted deployment as the final state. If **@home-PC** is unavailable, perform read-only diagnosis where possible and tell the user that a lasting development change must wait for access to the repository.

## Runtime documentation on Home Assistant

Before starting voice-stack maintenance, read:

- `/config/voice_stack_docs/README.md`
- `/config/voice_stack_docs/CURRENT_STATE.md`
- `/config/voice_stack_docs/PROJECT_GOAL.md`

These are convenient runtime mirrors. The Home PC Git repository remains authoritative.

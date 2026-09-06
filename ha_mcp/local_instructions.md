# Local development governance: Home Assistant voice stack

These optional instructions can be loaded by HA-MCP when an agent is maintaining this voice stack.

## Source of truth

The authoritative source is the Git checkout of this repository. Home Assistant contains deployed runtime copies only; do not treat files under `/addons` or `/config` as the only development source.

## Required workflow

1. Inspect `git status` and read the relevant public documentation (`README.md`, `docs/ARCHITECTURE.md`, `docs/P610.md`, `docs/DEBUGGING.md`, `docs/UPDATING.md`).
2. Make lasting changes in Git first. If an emergency runtime fix is made directly on Home Assistant, reproduce the final tested change in the repository.
3. Run the relevant syntax checks and tests. For voice-path changes, compare browser/WebRTC and the physical audio path when possible.
4. Back up the affected runtime files, deploy only the tested change, and verify the actual app/Core logs and status.
5. Document architectural or user-facing behavior changes before committing.
6. Create a meaningful Git commit for every completed logical change.

## Safety and exclusions

Never commit or copy into project memory API keys, access tokens, SSH keys, passwords, `.env`, Home Assistant `.storage`, databases, conversation transcripts, raw household voice recordings, cookies, or credentials.

Technical logs may be summarized, but secrets and private user speech must not be stored in Git. Audio-debug data is temporary diagnostic material and should remain outside the repository.

If the source checkout is unavailable, prefer read-only diagnosis over making a lasting untracked runtime change.

## Runtime documentation

A deployment may mirror selected Markdown documentation into `/config/voice_stack_docs/` for maintenance convenience. Such mirrors are not authoritative; the Git repository remains the source of truth.

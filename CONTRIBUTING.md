# Contributing

Changes that touch realtime audio should include a reproducible test or diagnostic evidence when practical.

Before opening a pull request:

1. do not include secrets or session recordings;
2. run Python syntax checks;
3. run the relevant regression tests;
4. run JavaScript syntax checks for modified cards;
5. describe whether the change affects browser, P610, Gemini, MCP, or all paths;
6. include before/after latency or PCM-gap measurements for performance changes;
7. explain rollback behavior for changes to the base image or build-time patches.

Keep P610-specific latency tuning isolated from the browser path unless there is evidence that the same setting improves both transports.

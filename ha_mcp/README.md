# HA-MCP local governance overlay

HA-MCP itself is an upstream component. This directory stores only the local, version-controlled overlay used by this Home Assistant voice stack.

## Files

- `local_instructions.md` — text appended to MCP server instructions during embedded-server startup.
- `patches/embedded_server_local_instructions.patch` — minimal patch that loads the local instruction file from HA-MCP's persistent config directory.

## Home Assistant deployment

- Instruction file: `/config/.ha_mcp/local_instructions.md`
- Patched runtime file: `/config/custom_components/ha_mcp_tools/embedded_server.py`

The patch reads the path through `self._config_dir`; it does not hard-code `/config` in Python.

## Update rule

After upgrading the HA-MCP custom component:

1. inspect the new `embedded_server.py`;
2. check whether local instruction support already exists upstream;
3. dry-run the patch against the new file;
4. adapt the overlay instead of overwriting the new component with an old complete file;
5. reload the HA-MCP server;
6. verify the MCP initialize response contains the heading `Local development governance: Home Assistant voice stack`;
7. update documentation and commit the result.

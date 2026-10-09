"""The `src/web` package: the second inbound port beside `src/tools` (AD-14).

This package holds thin FastAPI adapters, one module per capability, mirroring the
existing `src/tools` MCP-surface convention. All business logic lives in
`src/services`; modules here only translate HTTP requests into calls against that
shared service layer and render the result.

The web process runs as its own single ASGI worker (AD-21), started separately
from — and never sharing in-process state with — the MCP stdio process that
`src/tools` serves.
"""

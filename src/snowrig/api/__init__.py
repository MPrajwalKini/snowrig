"""Optional REST API (pip install snowrig[api]) so platforms with no
native Snowflake driver — a low-code tool, a Zapier/n8n workflow, curl,
a JS frontend — can run SQL through a named snowrig profile over plain
HTTP. See snowrig.api.server.build_app.

This is the opposite direction from snowrig.publishers: that pushes a
profile *into* another platform's own connection object. This instead
keeps the connection inside the process running `snowrig serve` and lets
callers reach it over HTTP, so the private key never leaves that process.
"""
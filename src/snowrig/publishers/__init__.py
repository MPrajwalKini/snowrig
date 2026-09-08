"""Publishers push a snowrig Profile *into* other platforms' own connection
objects, via that platform's REST API — the reverse direction from
`snowrig serve`, which exposes Snowflake *out* over HTTP.

Each publisher is optional and lives behind its own pip extra
(e.g. `pip install snowrig[idmc]`) so the base install stays free of
platform-specific HTTP client dependencies.
"""
## CONNX MCP Server

Always load and use the `connx-sql-expert` skill before using the CONNX MCP Server.

## CONNX queries

When using the `connx-sql-expert` skill:

- Read the catalog first (the `file:///catalog.md` resource, or the `read_catalog` tool if the resource isn't available). Don't take column names from `describe_table` or from memory.
- Write the PRE-FLIGHT block in full before each `read_data` call, then repeat it word for word, as a fenced code block, at the top of the message that presents the results. Text before a tool call may show up only as a summary, and a summary doesn't count.

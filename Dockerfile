FROM python:3.12-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir ".[mcp]"
# Glama builds this image and introspects the stdio MCP server over stdin/stdout.
ENTRYPOINT ["feeless402", "mcp"]

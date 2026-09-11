FROM python:3.12-slim

WORKDIR /app

# Install the project itself so the image does not depend on the host checkout.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Keep connection profiles outside the image. They contain database passwords.
RUN mkdir -p /data && chmod 700 /data
ENV MYSQL_CONNECTIONS_FILE=/data/connections.json

EXPOSE 8000

CMD ["python", "-m", "mysql_mcp.mcp_server", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000", "--path", "/mcp", "--stateless-http"]

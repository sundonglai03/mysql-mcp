FROM python:3.12-slim

ARG UV_VERSION=0.12.5
RUN pip install --no-cache-dir "uv==$UV_VERSION"

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    UV_CACHE_DIR=/app/.cache/uv

# 依赖缓存挂在当前目录下的 /app/.cache/uv（见上面的 UV_CACHE_DIR）：
# 依赖层重建时不会回 PyPI 重下。
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/app/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/app/.cache/uv \
    uv sync --frozen --no-dev

# 用户与属主单独一层：此时缓存挂载已释放，chown 不会连 /app/.cache 里的
# 缓存文件一起扫（否则每次构建都要多走一遍几百 MB 的目录树）。
RUN useradd --create-home --uid 1000 mysql-mcp \
    && chown -R mysql-mcp:mysql-mcp /app

USER mysql-mcp
EXPOSE 8000

CMD ["python", "-m", "mysql_mcp.mcp_server", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000", "--path", "/mcp", "--stateless-http"]

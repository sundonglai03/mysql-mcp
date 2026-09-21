FROM python:3.12-slim

ARG UV_VERSION=0.12.5
RUN pip install --no-cache-dir "uv==$UV_VERSION"

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# cache mount 把 uv 的下载缓存（约定路径 /root/.cache/uv）挂到宿主机构建缓存上：
# 依赖层重建时不会回 PyPI 重下，缓存本身不进镜像层。
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# 用户与属主单独一层：与依赖安装解耦，改 uid 或加用户不会连带重装依赖。
RUN useradd --create-home --uid 1000 mysql-mcp \
    && chown -R mysql-mcp:mysql-mcp /app

USER mysql-mcp
EXPOSE 8000

CMD ["python", "-m", "mysql_mcp.mcp_server", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000", "--path", "/mcp", "--stateless-http"]

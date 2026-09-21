FROM python:3.12-slim

ARG UV_VERSION=0.12.5
RUN pip install --no-cache-dir "uv==$UV_VERSION"

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# 先只 COPY 锁文件装依赖：uv.lock / pyproject.toml 不变时，改源码不会触发重装。
# 构建机装了 buildx（Docker 23+ 自带，或 docker-buildx-plugin）后，可以给这两条
# RUN 加上 --mount=type=cache,target=/root/.cache/uv，让锁文件变更后的重装也复用
# 宿主机的 uv 缓存。
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# 用户与属主单独一层：与依赖安装解耦，改 uid 或加用户不会连带重装依赖。
RUN useradd --create-home --uid 1000 mysql-mcp \
    && chown -R mysql-mcp:mysql-mcp /app

USER mysql-mcp
EXPOSE 8000

CMD ["python", "-m", "mysql_mcp.mcp_server", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000", "--path", "/mcp", "--stateless-http"]

FROM python:3.12-slim

ARG UV_VERSION=0.12.5
RUN pip install --no-cache-dir "uv==$UV_VERSION"

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# 先只 COPY 锁文件装依赖：uv.lock / pyproject.toml 不变时，改源码不会触发重装。
# --mount=type=cache 把 uv 的下载缓存（约定路径 /root/.cache/uv）挂到宿主机的
# 构建缓存上：锁文件变更后重装也复用缓存，不必重新下载 wheel。
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

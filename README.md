# mysql-mcp

一个支持 stdio 和 Streamable HTTP 的 MySQL MCP Server。模型可以通过工具查询表、执行受控 SQL，以及插入、更新和删除数据。

## 快速开始

```bash
cd /path/to/mysql-mcp
uv sync
```

### stdio（桌面 MCP 客户端）

```json
{
  "mcpServers": {
    "mysql-mcp": {
      "command": "/path/to/mysql-mcp/.venv/bin/python",
      "args": ["-m", "mysql_mcp"]
    }
  }
}
```

使用 `.venv/bin/python` 的绝对路径；不要在客户端配置中依赖 `cwd` 或 `uv run`。

### Streamable HTTP

```bash
.venv/bin/python -m mysql_mcp.mcp_server \
  --transport streamable-http --host 127.0.0.1 --port 8000 --path /mcp
```

MCP 地址：`http://127.0.0.1:8000/mcp`。这是 MCP 协议端点，不是普通网页。
可信内网可以使用 HTTP；跨不可信网段时建议设置 `MYSQL_MCP_AUTH_TOKEN` 或使用
`--auth-token`。没有证书也可以先用 HTTP + Token，HTTPS 不是运行前提。

### Docker

```bash
docker compose up -d --build
docker compose logs -f mysql-mcp
docker compose down
```

镜像名为 `sundonglai/mysql-mcp:latest`，容器名为 `mysql-mcp`。默认监听
`127.0.0.1:8000`。服务不保存连接档案，数据库凭据由每次调用提供。

## 连接数据库

每次业务调用都直接传入数据库凭据：

```text
list_tables(credentials={
  "host": "db.example.com",
  "user": "reader",
  "password": "...",
  "database": "app"
})
```

服务只在内存中使用凭据，连接关闭后不会保存密码。

## 工具

| 工具 | 作用 |
| --- | --- |
| `health_check` | 检查连接和 MySQL 版本 |
| `get_databases` | 列出数据库 |
| `list_tables` | 列出表 |
| `describe_table` | 查看表结构 |
| `fetch_table` | 查询表数据，支持结构化过滤和排序 |
| `count_rows` | 统计行数 |
| `execute_query` | 执行单条 SQL，默认只读 |
| `insert_row` / `update_rows` / `delete_rows` | 数据变更 |

## 安全边界

- `execute_query` 默认只允许 `SELECT`、`SHOW`、`DESCRIBE`、`EXPLAIN`。
- 永远拒绝多语句 SQL；表名、字段名经过标识符校验，值使用参数绑定。
- 查询默认返回 50 行，服务端上限 500 行。
- 结构化更新和删除默认最多影响 1000 行，硬上限 10000 行。
- `read_only=true` 的连接拒绝写操作。
- `execute_query(read_only=false)` 仍是高权限通道，真正的权限控制应使用 MySQL 账号权限。
- HTTP 模式默认只绑定回环地址；远程部署必须增加认证和 HTTPS。

## 项目结构

```text
src/mysql_mcp/
├── mcp_server.py   # MCP transport 入口
├── server.py       # 工具和 SQL 策略
├── client.py       # MySQL 连接生命周期
└── connections.py  # 连接参数校验和 TLS 配置
```

## 开发

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
docker compose config
```

不要提交 `.venv/`、Docker 数据卷或任何密码文件。

## License

MIT

# mysql-mcp

一个基于 Python 的 MySQL MCP Server，适合接入 WorkBuddy、Claude Desktop、Cursor、VS Code 等支持 MCP 的客户端。

## 功能概览

- 健康检查：测试数据库连接是否正常
- 列出所有数据库：`get_databases()`
- 列出当前数据库中的表：`list_tables()`
- 查询表数据：`fetch_table(table_name, limit=50, where=None, order_by=None)`
- 统计记录数：`count_rows(table_name, where=None)`
- 查看表结构：`describe_table(table_name)`
- 插入数据：`insert_row(table_name, row)`
- 更新数据：`update_rows(table_name, updates, where, where_params=None)`
- 删除数据：`delete_rows(table_name, where, where_params=None)`
- 执行 SQL：`execute_sql(sql, limit=50, read_only=True)`
- 只读查询执行：`execute_query(sql, limit=50, read_only=True)`

## 项目结构

```text
mysql-mcp/
├── pyproject.toml
├── README.md
├── workbuddy-mcp.json
└── src/
    └── mysql_mcp/
        ├── __init__.py
        ├── __main__.py
        └── server.py
```

## 安装

推荐直接使用 uv：

```bash
cd /Users/sundonglai/ai/vscode/study/PYTHON/mysql-mcp
uv sync
uv run python -m mysql_mcp
```

如果你想用传统 venv，也可以：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

## 环境变量

启动前设置以下环境变量：

```bash
export MYSQL_HOST=localhost
export MYSQL_PORT=3306
export MYSQL_USER=root
export MYSQL_PASSWORD=your_password
export MYSQL_DATABASE=your_database
export MYSQL_CHARSET=utf8mb4
```

也可以直接在 MCP 配置中放到 `env` 里。

## 启动方式

直接运行：

```bash
uv run python -m mysql_mcp
```

或者：

```bash
uv run mysql-mcp
```

如果你已经在项目目录里并且环境已同步，也可以直接运行：

```bash
python -m mysql_mcp
```

## WorkBuddy 配置示例

项目里已提供配置文件：[workbuddy-mcp.json](workbuddy-mcp.json)

示例内容：

```json
{
  "mcpServers": {
    "mysql-mcp": {
      "command": "/Users/sundonglai/ai/vscode/study/PYTHON/mysql-mcp/.venv/bin/python",
      "args": ["-m", "mysql_mcp"],
      "env": {
        "MYSQL_HOST": "localhost",
        "MYSQL_PORT": "3306",
        "MYSQL_USER": "root",
        "MYSQL_PASSWORD": "your_password",
        "MYSQL_DATABASE": "your_database",
        "MYSQL_CHARSET": "utf8mb4"
      }
    }
  }
}
```

把这里的数据库信息替换成你自己的实际值即可。

## 工具示例

```python
# 查询所有表
list_tables()

# 查询所有数据库
get_databases()

# 查看表结构
describe_table("users")

# 查询前 20 条数据
fetch_table("users", limit=20)

# 统计数量
count_rows("users")

# 插入一条数据
insert_row("users", {"name": "alice", "age": 18})

# 更新数据
update_rows("users", {"age": 19}, "id = %s", [1])

# 删除数据
delete_rows("users", "id = %s", [1])

# 执行只读 SQL
execute_query("SELECT * FROM users LIMIT 10")

# 执行 SQL（默认只读保护）
execute_sql("SELECT * FROM users LIMIT 10")
```

## 安全说明

- 默认 `read_only=True`，防止误删误改。
- `execute_query` / `execute_sql` 会拒绝非查询语句（除非你显式改成 `read_only=False`）。
- `update_rows` / `delete_rows` / `insert_row` 是写操作，建议仅在本地开发或受控环境中使用。

## 常见问题

### 1. 报 `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`

这是因为你装的是 MCP 2.x，而旧代码仍在用 v1 的 FastMCP API。当前项目已经修正为兼容 mcp 2.x。

### 2. `MYSQL_DATABASE` 不能为空

确保在启动前设置了数据库名环境变量，或者在 `workbuddy-mcp.json` 的 `env` 中配置了 `MYSQL_DATABASE`。

### 3. 连接不上 MySQL

检查：
- MySQL 服务是否启动
- 主机、端口、用户名、密码是否正确
- 用户是否有对应数据库权限

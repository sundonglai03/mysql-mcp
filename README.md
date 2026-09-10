# mysql-mcp

一个 Python 写的 MySQL MCP Server，接进 Claude Desktop / Cursor / VS Code 等支持 MCP 的客户端后，模型可以直接查库。

支持**同时连多个数据库**：连接信息存成命名档案，模型每次调用只多传一个名字（`connection="mydb"`），不用重启进程，密码也不会进对话上下文。

遇到还没存过的库，可以**首次连接时把登录信息随调用传一次**（`credentials`），连成功后自动写进档案；之后同样只传名字。

## 安装

```bash
cd /path/to/mysql-mcp
uv sync
```

或者用传统 venv：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

两种方式得到的 `.venv` 一样。editable 安装，改 `src/` 下的代码不用重装。

## 接入 MCP 客户端

在客户端的 MCP 配置文件里加一条（Claude Desktop 是 `claude_desktop_config.json`，Cursor 是 `~/.cursor/mcp.json`）：

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

两个要点：

- **`command` 用 `.venv/bin/python` 的绝对路径**，不要用 `uv run`——`uv run` 每次启动都要校验环境、多套一层进程，而客户端渲染 stdio 配置时会丢掉 `cwd`，相对路径一定找不到。uv 只用来 `uv sync` 建环境。
- **没有 `env`**。服务端不读任何连接相关的环境变量：数据库地址、账号、密码一律来自 `~/.mysql-mcp/connections.json`（跟项目目录无关），或首次调用时随 `credentials` 传一次。

配置写好后重启客户端，工具就出现在对话里了。

## 使用

### 1. 接入一个新库

**方式 A — 首次连接时带上凭据（最省事）**

直接查，把登录信息放进 `credentials`，`connection` 给个名字：

```python
list_tables(connection="mydb", credentials={
    "host": "db.example.com", "user": "root",
    "password": "…", "database": "mydb",
})
```

服务端会先用这些信息真连一次，**连成功了才写进档案文件**——所以它本身就是一次凭据校验，不会出现「保存成功但密码是错的」。返回里会多一个 `connection_saved` 字段，告诉你落在了哪个文件。之后只传名字就行：

```python
list_tables(connection="mydb")
```

**方式 B — 显式保存**

```
save_connection(name="mydb", host="db.example.com", user="root",
                password="…", database="mydb", read_only=true)
```

和方式 A 的区别：`save_connection` **不试连**，凭据写错了要到第一次查询才暴露；好处是可以顺便设 `read_only`、`max_affected_rows` 这些策略字段。

**方式 C — 自己手改文件**

`~/.mysql-mcp/connections.json`（目录 `700`、文件 `600`，`_` 开头的键当注释）：

```json
{
  "_comment": "connection profiles for mysql-mcp",
  "mydb": {
    "host": "db.example.com",
    "user": "root",
    "password": "…",
    "database": "mydb",
    "read_only": true,
    "description": "read-only replica"
  }
}
```

> **同名不会被随便覆盖。** 如果新信息指向的 `host` / `port` / `user` 和已存档案不一致，调用会被拒绝（`CONNECTION_CONFLICT`）——否则一个名字被悄悄改指到别的机器，之后所有用这个名字的查询都会打到错误的库。确实要改指向，用 `save_connection(..., overwrite=true)` 显式说明；指向同一台机器时可以直接更新密码。
>
> ⚠️ `save_connection` 省略 `password` / `database` / `charset` 时是**按默认值整体替换，不是保留原值**。所以改端口、改描述时，要把 `password` 和 `database` 原样再传一遍，否则凭据会被清空（返回里 `has_password` 会变成 `false`）。`read_only` / `max_affected_rows` / `description` 则是省略即保留。
>
> `credentials` **不能**放开策略：`read_only` / `max_affected_rows` 只能通过 `save_connection` 设置，已有档案的值不会被内联凭据覆盖或放宽。

`read_only: true` 的档案会拒绝一切写操作（且在**建连之前**就拒）；`max_affected_rows` 可以给某个库单独钉一个更严的变更行数上限。

`list_connections()` 查看当前有哪些名字，**返回里不含密码**。档案是每次调用时现读的，改完立即生效，不用重启。

### 2. MCP 调用

13 个工具，除 `list_connections` / `save_connection` / `delete_connection` 外都接受 `connection` 和 `credentials` 两个参数：

| 工具 | 说明 |
| --- | --- |
| `health_check(connection=None, credentials=None)` | 探活，返回连接名、主机、库名、MySQL 版本 |
| `get_databases(connection=None, credentials=None)` | 列出所有可见数据库 |
| `list_tables(connection=None, credentials=None)` | 列出当前库的所有表 |
| `describe_table(table_name, connection=None, credentials=None)` | 表结构 |
| `fetch_table(table_name, limit=50, where=None, order_by=None, where_params=None, connection=None, credentials=None)` | 查询表数据 |
| `count_rows(table_name, where=None, where_params=None, connection=None, credentials=None)` | 统计行数 |
| `execute_query(sql, limit=50, read_only=True, connection=None, credentials=None)` | 执行单条 SQL，默认只读 |
| `insert_row(table_name, row, connection=None, credentials=None)` | 插入一行 |
| `update_rows(table_name, updates, where, where_params=None, max_affected_rows=1000, connection=None, credentials=None)` | 更新 |
| `delete_rows(table_name, where, where_params=None, max_affected_rows=1000, connection=None, credentials=None)` | 删除 |
| `list_connections()` | 列出已存连接，**不含密码** |
| `save_connection(name, host, port=3306, user="root", password="", database=None, charset="utf8mb4", read_only=None, max_affected_rows=None, description=None, overwrite=False)` | 新建/覆盖连接档案 |
| `delete_connection(name)` | 删除连接档案 |

`credentials` 是一个对象，字段和档案一致：`{"host": 必填, "port", "user", "password", "database", "charset"}`。它必须和 `connection` 一起用——那个名字就是它要存的位置。

调用示例：

```python
# 首次接入一个还没存过的库：带上凭据，连成功后自动记住
list_tables(connection="mydb", credentials={
    "host": "db.example.com", "user": "root",
    "password": "…", "database": "mydb",
})

# 结构 + 数据
describe_table("users", connection="mydb")
fetch_table(
    "users",
    limit=20,
    where=[
        {"field": "status", "op": "=", "value": "active"},
        {"logic": "AND", "field": "age", "op": ">=", "value": 18},
    ],
    order_by=[{"field": "created_at", "direction": "DESC"}],
    connection="mydb",
)

# 统计 / 插入 / 更新 / 删除
count_rows("users", where=[{"field": "status", "op": "=", "value": "active"}], connection="mydb")
insert_row("users", {"name": "alice", "age": 18, "status": "active"}, connection="mydb")
update_rows("users", {"age": 19}, where=[{"field": "id", "op": "=", "value": 1}], connection="mydb")
delete_rows("users", where=[{"field": "id", "op": "=", "value": 1}], connection="mydb")

# 只读 SQL
execute_query("SELECT * FROM users LIMIT 10", connection="mydb")

# 换库：只是换个名字（凭据已存过，不用再传）
fetch_table("orders", limit=10, connection="warehouse")
health_check(connection="warehouse")
```

**每次调用都要给 `connection`**——没有默认连接。忘了给会返回 `CONNECTION_ERROR`，并在 `error` 里列出当前已存的名字，不会退化成去连 localhost。

成功时返回 `ok: true` 加数据（若这次调用顺带存了新档案，还会多一个 `connection_saved`），失败时返回 `ok: false` 加 `error` 和一个 `code`（如 `CONNECTION_ERROR`、`CONNECTION_CONFLICT`、`CONNECTION_READ_ONLY`、`MUTATION_LIMIT_EXCEEDED`）。

注意返回形状不统一：`list_tables` / `get_databases` / `describe_table` **成功时**直接返回数组，其余工具返回 `{"ok": true, ...}` 信封；**失败时一律是信封**。所以判断成败不能只看有没有 `ok`。

`where` / `order_by` 不接受原始 SQL 片段，必须是结构化对象——字段名走白名单加反引号，取值全部走 `%s` 参数绑定。运算符支持 `=`、`!=`、`<>`、`>`、`>=`、`<`、`<=`、`LIKE`、`IN`、`IS`、`IS NOT`；`logic` 从第二条子句起生效（首条写 `OR` 会明确报错）。取值里出现 `;`、`--`、`/*`、`*/` 会直接报错（参数绑定本身已防注入，这层只是顺手挡掉可疑字面量）。

### 3. 连接的解析规则

1. `connection="<名字>"` **加** `credentials` → 用内联信息连一次，**连成功后**才把这个名字存成档案；
2. 只有 `connection="<名字>"` → 从档案文件取；
3. 两个都没有 → 返回 `CONNECTION_ERROR` 并列出已存名字，**不会偷偷去连 localhost**。

没有「默认连接」，也不读任何连接相关的环境变量（`MYSQL_HOST` 之类全部不再生效）。

`credentials` 单独给（不带 `connection`）也会被拒绝——没有名字就无处可存。

每次调用都是新建连接、用完即关，进程里没有「当前连接」状态，并发和重启都不会串。

## 环境变量

只有一个，跟项目目录无关：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MYSQL_CONNECTIONS_FILE` | `~/.mysql-mcp/connections.json` | 档案文件路径（测试用；平时不用设） |

数据库地址、账号、密码、超时、TLS 全部来自档案文件里的字段，不走环境变量。

## 默认的安全约束

- `execute_query` 默认只读，且 `INTO OUTFILE`、`FOR UPDATE`、`EXPLAIN ANALYZE` 这类隐式写操作一律拒绝；多语句永远不执行。
- 结果集默认 50 行、封顶 500 行；写操作默认最多影响 1000 行（硬顶 10000），超出直接拒绝。
- 密码只存在于档案文件（`600`）里，任何工具返回都不含密码；`credentials` 只在首次调用时进上下文一次。
- 档案只在**连接成功之后**才写入；把已有名字改指向另一台主机需要显式 `overwrite=true`。
- 远端库的 TLS 参数（`ssl_ca` / `ssl_cert` / `ssl_key` / `ssl_verify_cert` / `ssl_disabled` …）写在该档案的字段里，每个库各管各的，不共用全局配置。
- 主机没有白名单：`connections.json` 是唯一的准入点，写好之后就只往这些库连。文件本身权限 `600`，别提交进版本库。
- 已知限制：标识符白名单只收 `[A-Za-z_][A-Za-z0-9_]*`，所以 `db.table`、中文表名、含 `$` 的字段会被拒；`where` 只支持扁平 AND/OR，不支持括号嵌套。

### 别把它当权限边界

`read_only` 与 `max_affected_rows` 只作用于**结构化写入工具**（`insert_row` / `update_rows` / `delete_rows`）。`execute_query(sql, read_only=False)` 是一条无护栏通道：不带 WHERE 的 `DELETE`、超出行数上限的批量更新都会照常执行。

要真正限制写入，请在 MySQL 侧用**只读账号**（只授 `SELECT`），把它写进对应的档案；工具层的开关只当防手滑的第一道。

## 开发

```bash
.venv/bin/python -m pytest    # 171 个用例
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

测试用临时目录做档案文件，不会碰真实的 `~/.mysql-mcp/connections.json`。

## License

MIT

---
title: TG-SignPulse
emoji: 🤖
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# TG-SignPulse 部署说明

这是我当前整理后的可部署版本，推荐部署方式为：

**Hugging Face Spaces（Docker） + Supabase（PostgreSQL）**

当前版本已经完成并验证：

- 可通过 `DATABASE_URL` 连接 Supabase PostgreSQL
- Telegram `session_string` 可持久化到数据库
- 任务配置、任务历史、聊天缓存、系统配置已改为数据库优先持久化
- Hugging Face Space 重启后，已写入数据库的数据仍可恢复

## 1. 推荐部署方案

推荐直接使用：

- 平台：Hugging Face Spaces
- SDK：Docker
- 数据库：Supabase PostgreSQL

不建议依赖 Hugging Face 本地临时文件做长期持久化。  
容器重启、重建或迁移后，本地文件不可靠；数据库才是稳定持久化方案。

## 2. 当前已持久化到 Supabase 的数据

当前代码里，以下数据已经改为以 Supabase/PostgreSQL 为主存储：

- Telegram 登录 `session_string`
- 账号备注 `remark`
- 账号代理 `proxy`
- 签到任务配置
- 监控任务配置
- 任务执行历史
- 聊天缓存
- AI 配置
- 全局设置
- Telegram API 配置

说明：

- 代码仍会保留部分本地文件镜像，例如 `config.json`、`.openai_config.json`、`.telegram_api.json`
- 这些镜像文件现在主要用于兼容旧运行逻辑，不再作为主存储来源
- 真正需要跨重启保留的数据，已经优先写入数据库

## 3. Hugging Face 需要配置的 Secrets

到 Hugging Face Space：

`Settings -> Variables and secrets`

建议把敏感配置全部放到 `Secrets`，不要和 `Variables` 重名。

### 必填项

| 名称 | 示例 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://postgres:password@host:5432/postgres` | Supabase PostgreSQL 连接串 |
| `APP_SECRET_KEY` | `your_random_secret_key` | 面板加密与会话安全 |
| `ADMIN_PASSWORD` | `your_admin_password` | 管理员密码 |

### 强烈建议配置

| 名称 | 推荐值 | 说明 |
| --- | --- | --- |
| `TG_SESSION_MODE` | `string` | 推荐。Telegram session 走数据库持久化 |
| `APP_HOST` | `0.0.0.0` | 让容器可被 Hugging Face 正常访问 |
| `TG_GLOBAL_CONCURRENCY` | `1` | 先用保守并发，稳定优先 |
| `TG_SESSION_NO_UPDATES` | `0` | 默认保留 updates |
| `TZ` | `Asia/Shanghai` | 时区，可按需修改 |

### 推荐示例

```env
DATABASE_URL=postgresql://postgres:your_password@your_host:5432/postgres
APP_SECRET_KEY=your_random_secret_key
ADMIN_PASSWORD=your_admin_password
TG_SESSION_MODE=string
APP_HOST=0.0.0.0
TG_GLOBAL_CONCURRENCY=1
TG_SESSION_NO_UPDATES=0
TZ=Asia/Shanghai
```

## 4. Supabase 连接串注意事项

请在 Supabase 后台获取 PostgreSQL URI。

路径一般在：

`Project Settings -> Database`

如果密码中包含特殊字符，例如：

- `@`
- `:`
- `/`
- `?`
- `#`

必须先做 URL 编码，否则 `DATABASE_URL` 可能解析失败。

例如 `@` 应编码为：

```text
%40
```

## 5. Hugging Face 部署步骤

### 第一步：创建 Space

创建一个新的 Hugging Face Space，选择：

- SDK：`Docker`

### 第二步：上传项目代码

确保仓库根目录至少包含：

- `frontend/`
- `backend/`
- `tg_signer/`
- `docker/`
- `Dockerfile`
- `pyproject.toml`

### 第三步：配置 Secrets

在 Space 的 `Settings -> Variables and secrets` 中填入上面的环境变量。

### 第四步：重启 Space

如果你改了环境变量，推荐执行：

`Factory reboot`

这样可以避免旧缓存影响启动。

### 第五步：查看启动日志

如果日志中应用正常启动，没有数据库连接报错，通常说明部署成功。

## 6. 如何确认数据库持久化已经生效

你可以在 Supabase 的 SQL Editor 里执行下面这些查询。

### 6.1 查看有哪些表

```sql
select table_name
from information_schema.tables
where table_schema = 'public'
order by table_name;
```

你当前重点应该能看到：

- `accounts`
- `persisted_states`

### 6.2 查看账号登录是否已持久化

```sql
select
  account_name,
  session_string is not null as has_session,
  length(session_string) as session_length,
  proxy,
  remark,
  updated_at
from accounts
order by updated_at desc;
```

如果：

- `has_session = true`
- `session_length` 有值

说明 `session_string` 已经进库。

### 6.3 查看任务配置是否已持久化

```sql
select
  category,
  item_key,
  scope,
  updated_at
from persisted_states
where category in ('sign_task', 'monitor_task')
order by updated_at desc;
```

### 6.4 查看任务历史是否已持久化

```sql
select
  item_key as task_name,
  scope as account_name,
  updated_at,
  left(payload, 200) as payload_preview
from persisted_states
where category = 'sign_task_history'
order by updated_at desc;
```

### 6.5 查看聊天缓存是否已持久化

```sql
select
  item_key as account_name,
  updated_at,
  left(payload, 200) as payload_preview
from persisted_states
where category = 'chat_cache'
order by updated_at desc;
```

### 6.6 查看系统配置是否已持久化

```sql
select
  category,
  item_key,
  scope,
  updated_at,
  left(payload, 200) as payload_preview
from persisted_states
where category in ('ai_config', 'global_settings', 'telegram_config')
order by updated_at desc;
```

### 6.7 查看单个签到任务的完整配置

假设：

- 任务名是 `task1`
- 账号名是 `tg01`

可以执行：

```sql
select payload
from persisted_states
where category = 'sign_task'
  and item_key = 'task1'
  and scope = 'tg01';
```

## 7. 如何验证“重启后数据还在”

最稳的方法：

1. 先执行上面的 SQL，确认数据库里已经有数据
2. 记录一下 `updated_at`
3. 重启 Hugging Face Space
4. 重启后重新执行相同 SQL
5. 如果记录还在，并且应用能正常读取，说明持久化成功

你之前已经实测确认：

- `accounts.session_string` 有非空值
- 重启后 Telegram 登录状态仍可恢复

这说明最关键的登录持久化链路已经打通。

## 8. 关于本地文件与数据库的关系

当前项目是：

**数据库作为主存储，本地文件作为兼容镜像**

也就是说：

- 应长期信任 Supabase 中的数据
- 不要把容器内本地文件当作唯一持久化来源
- 即使镜像文件被清空，只要数据库里还有数据，系统仍可恢复关键配置

## 9. 常见建议

### 建议 1：`TG_SESSION_MODE` 使用 `string`

在 Hugging Face 上推荐固定为：

```env
TG_SESSION_MODE=string
```

这样 Telegram 登录态会更适合容器环境下的数据库持久化。

### 建议 2：并发不要开太高

初始推荐：

```env
TG_GLOBAL_CONCURRENCY=1
```

先稳定运行，再考虑提高。

### 建议 3：不要依赖 HF 临时文件

不要把容器里的临时本地文件当成可靠持久化存储。  
长期保留的数据，应以 Supabase 为准。

## 10. 本地 Docker 备用运行方式

如果你之后要在本地或 VPS 上单独运行，也可以直接用 Docker。

```bash
docker run -d \
  --name tg-signpulse \
  --restart unless-stopped \
  -p 8080:8080 \
  -v $(pwd)/data:/data \
  -e TZ=Asia/Shanghai \
  -e APP_SECRET_KEY=your_secret_key \
  tg-signpulse-image:latest
```

如果本地也想使用 Supabase，同样只需要额外传入：

```env
DATABASE_URL=postgresql://...
```

## 11. 项目来源

本项目基于原项目进行修改和扩展：

- 原项目：[`tg-signer`](https://github.com/amchii/tg-signer)
- 作者：[`amchii`](https://github.com/amchii)

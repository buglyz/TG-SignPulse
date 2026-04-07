# TG-SignPulse 部署与配置备份

个人部署备忘录。当前采用的稳定运行方案为：**Hugging Face Spaces (Docker) + Supabase (PostgreSQL)**。

> **注意**：不使用 Hugging Face 的 Storage Buckets 持久化卷，直接连接外部数据库以避免启动时的文件锁死问题。

## 1. 部署环境与前置准备

- **托管平台**: Hugging Face Spaces
- **环境模板**: Docker (Blank)
- **外部数据库**: Supabase (PostgreSQL)

### 获取 Supabase 连接串
在 Supabase 项目的 `Project Settings` -> `Database` 中获取 URI 连接串。
格式：`postgresql://postgres:[密码]@[主机]:5432/postgres`
*注意：如果密码包含特殊字符（如 `@`），必须进行 URL 编码（如改为 `%40`）。*

## 2. 环境变量配置 (关键)

在 Hugging Face Space 的 `Settings` -> `Variables and secrets` 中配置。

**⚠️ 严格注意：** 所有敏感配置必须**仅**添加在 **Secrets** 列表中。绝对不能在 Variables 列表中添加同名变量，否则会引发 `Collision on variables and secrets names` 报错并导致容器崩溃。

必须添加的 Secrets：

| 变量名 (Name) | 变量值 (Value) 示例 | 说明 |
| :--- | :--- | :--- |
| `DATABASE_URL` | `postgresql://postgres:pass...` | Supabase 数据库连接字符串 |
| `APP_SECRET_KEY` | `your_random_secret_string` | 应用安全密钥，用于加密或鉴权 |
| `TZ` | `Asia/Shanghai` | 设定容器时区为北京时间 |

## 3. Hugging Face 部署流程

1. 在 Hugging Face 创建 Space，选择 Docker 环境。
2. 将代码（包含 `Dockerfile`）推送到该 Space。
3. 进入 `Settings` 配置上述 **Secrets**。
4. 如果遇到配置冲突或修改了环境变量，点击右上角 `Settings` -> **Factory reboot** 强制清理缓存并重启。
5. 观察运行日志，出现 `INFO: Application startup complete` 且没有超时错误，即代表后端及数据库连接成功。

## 4. 运行状态与避坑记录

* **持久化存储冲突**：**不要**在 Space 中挂载 Storage Buckets 到 `/data` 目录。挂载会导致残留的 SQLite 缓存或损坏的 Session 文件阻塞启动过程（卡在 `Application Startup`）。
* **Telegram Session 掉线机制**：由于移除了本地持久化存储，所有的任务配置保存在 Supabase（不丢失），但 Telegram 的登录 Session 保存在临时容器中。**每次 Space 重启、休眠唤醒或代码重构后，都需要在 Web 面板重新扫码/验证登录 TG。**
* **TgCrypto 警告**：启动日志提示 `TgCrypto is missing!` 为正常现象。在云端精简 Docker 镜像中缺少 C 编译环境，系统会自动回退到纯 Python 模式运行，不影响实际功能。

## 5. 本地备用部署命令 (Docker)

如果后续需要迁回本地 VPS 运行，使用自带的 SQLite 数据库（无需配置 DATABASE_URL），可以直接使用以下命令：

```bash
docker run -d \
  --name tg-signpulse \
  --restart unless-stopped \
  -p 8080:8080 \
  -v $(pwd)/data:/data \
  -e TZ=Asia/Shanghai \
  -e APP_SECRET_KEY=你的随机密钥 \
  tg-signpulse-image:latest

# TG-SignPulse

[中文说明](README.md)

TG-SignPulse is a Telegram automation panel with a recommended deployment model of:

**Hugging Face Spaces (Docker) + Supabase (PostgreSQL)**

This version has been verified for:

- Supabase/PostgreSQL connection through `DATABASE_URL`
- Telegram `session_string` persistence in the database
- Database-first persistence for task configs, task history, chat cache, and system configs
- Recovery of persisted data after Hugging Face Space restarts

## Recommended Deployment

Use:

- Platform: Hugging Face Spaces
- SDK: Docker
- Database: Supabase PostgreSQL

Do not rely on container-local temporary files for long-term persistence.

## What Is Persisted to Supabase

The following data is now stored primarily in PostgreSQL:

- Telegram `session_string`
- Account `remark`
- Account `proxy`
- Sign task configs
- Monitor task configs
- Task execution history
- Chat cache
- AI config
- Global settings
- Telegram API config

Some local files are still generated as compatibility mirrors, such as:

- `config.json`
- `.openai_config.json`
- `.telegram_api.json`

Those files are no longer the source of truth.

## Required Hugging Face Secrets

Open:

`Settings -> Variables and secrets`

Put sensitive values in `Secrets`.

### Required

| Name | Example | Description |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://postgres:password@host:5432/postgres` | Supabase PostgreSQL connection string |
| `APP_SECRET_KEY` | `your_random_secret_key` | Panel/session security secret |
| `ADMIN_PASSWORD` | `your_admin_password` | Admin password |

### Strongly Recommended

| Name | Recommended Value | Description |
| --- | --- | --- |
| `TG_SESSION_MODE` | `string` | Recommended for DB-backed Telegram session persistence |
| `APP_HOST` | `0.0.0.0` | Required for container accessibility in HF |
| `TG_GLOBAL_CONCURRENCY` | `1` | Start conservative for stability |
| `TG_SESSION_NO_UPDATES` | `0` | Keep updates enabled by default |
| `TZ` | `Asia/Shanghai` | Timezone |

### Example

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

## Supabase Connection String Notes

Get the PostgreSQL URI from:

`Project Settings -> Database`

If your password contains special characters such as `@`, `:`, `/`, `?`, or `#`, URL-encode them first.

For example:

```text
@ -> %40
```

## Hugging Face Deployment Steps

1. Create a new Space with `Docker`
2. Push this project to the Space
3. Configure the required Secrets
4. Run `Factory reboot` after env var changes
5. Check startup logs for successful app and DB initialization

## How To Verify Persistence In Supabase

Run these SQL queries in the Supabase SQL Editor.

### List tables

```sql
select table_name
from information_schema.tables
where table_schema = 'public'
order by table_name;
```

You should especially see:

- `accounts`
- `persisted_states`

### Check Telegram session persistence

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

If `has_session = true` and `session_length` is non-zero, the session is persisted.

### Check task config persistence

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

### Check task history persistence

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

### Check chat cache persistence

```sql
select
  item_key as account_name,
  updated_at,
  left(payload, 200) as payload_preview
from persisted_states
where category = 'chat_cache'
order by updated_at desc;
```

### Check system config persistence

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

### Check a single sign task

If task name is `task1` and account name is `tg01`:

```sql
select payload
from persisted_states
where category = 'sign_task'
  and item_key = 'task1'
  and scope = 'tg01';
```

## How To Verify Restart Persistence

The most reliable method:

1. Run the SQL queries above
2. Note the `updated_at` values
3. Restart the Hugging Face Space
4. Run the same SQL again
5. Confirm the records still exist and the app can still read them

Your deployment has already verified the key case:

- `accounts.session_string` is present
- Telegram login state survives restart

## Docker Fallback

You can also run it locally or on a VPS:

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

If you also want PostgreSQL persistence locally, pass:

```env
DATABASE_URL=postgresql://...
```

## Credits

This project is based on and heavily extended from:

- Original project: [tg-signer](https://github.com/amchii/tg-signer)
- Author: [amchii](https://github.com/amchii)

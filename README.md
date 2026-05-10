# nanobot 调度与操作指南

> 适用版本：pinterest-pod-agent，更新时间：2026-05-05

## 目录

1. [系统架构概览](#1-系统架构概览)
2. [数据库表与字段详解](#2-数据库表与字段详解)
3. [任务类型说明](#3-任务类型说明)
4. [通过 nanobot 指令操作](#4-通过-nanobot-指令操作)
5. [通过数据库直接操作](#5-通过数据库直接操作)
6. [服务启动与停止](#6-服务启动与停止)
7. [常见问题排查](#7-常见问题排查)

---

## 1. 系统架构概览

```
┌─────────────┐     自然语言指令      ┌──────────────────┐
│   nanobot   │ ────────────────────> │  MCP Server      │
│   (CLI)     │ <──────────────────── │  (16 个工具)      │
└─────────────┘    工具调用结果        └────────┬─────────┘
                                               │
                     ┌─────────────────────────┼─────────────────────────┐
                     │                         │                         │
                     ▼                         ▼                         ▼
              ┌────────────┐          ┌──────────────┐          ┌──────────────┐
              │ PostgreSQL │          │    Redis      │          │   Celery     │
              │ (9 张表)    │          │ (锁/队列)     │          │ (12 个任务)   │
              └────────────┘          └──────────────┘          └──────┬───────┘
                                                                       │
                                                               ┌───────┴───────┐
                                                               │  Playwright   │
                                                               │  + AdsPower   │
                                                               │  + Pinterest  │
                                                               └───────────────┘
```

**核心流程：** nanobot 通过 MCP 工具写入 `scheduled_task` 表 → 调度器扫描待执行任务 → 发送到 Celery Worker → Worker 通过 Playwright 操作 AdsPower 浏览器完成 Pinterest 自动化。

---

## 2. 数据库表与字段详解

> 数据库：PostgreSQL，连接信息在 `.env` 的 `DATABASE_URL`

### 2.1 social_account — 社交账号

记录每个被管理的 Pinterest 账号及其绑定的 AdsPower 浏览器环境。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Integer | 主键（自增） |
| `account_id` | String(64) | **账号唯一标识**，如 `test-account-1` |
| `platform` | String(40) | 平台，默认 `pinterest` |
| `display_name` | String(120) | 显示名称 |
| `adspower_profile_id` | String(120) | AdsPower 浏览器 profile ID，如 `k1buvn6c` |
| `proxy_region` | String(80) | 代理地区，如 `US` |
| `risk_status` | String(40) | 风控状态：`unknown` / `safe` / `warning` / `banned` |
| `created_at` | DateTime | 创建时间 |
| `updated_at` | DateTime | 更新时间 |

### 2.2 account_policy — 账户策略

每个账号的发布频率、时间窗口、暖机计划等限制策略。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Integer | 主键（自增） |
| `account_id` | String(64) | **账号唯一标识**（与 social_account 对应） |
| `daily_max_posts` | Integer | 每日最大发帖数，默认 3 |
| `min_post_interval_min` | Integer | 最小发帖间隔（分钟），默认 60 |
| `allowed_timezone_start` | String(5) | 允许发帖时间段开始，如 `09:00` |
| `allowed_timezone_end` | String(5) | 允许发帖时间段结束，如 `22:00` |
| `auto_reply_enabled` | Boolean | 是否启用自动回复，默认 false |
| `warmup_sessions_per_day` | Integer | 每日暖机会话数，默认 2 |
| `warmup_duration_min` | Integer | 每次暖机时长（分钟），默认 15 |
| `cooldown_until` | DateTime | 冷却期截止时间（用于临时冻结） |
| `created_at` / `updated_at` | DateTime | 创建/更新时间 |

### 2.3 campaign — 营销活动

定义内容营销活动，包括目标人群、产品类型、节日场景等。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Integer | 主键（自增） |
| `campaign_id` | String(64) | **活动唯一标识**，如 `spring-sale-2026` |
| `name` | String(160) | 活动名称 |
| `niche` | String(120) | 利基市场，如 `pet lovers`、`home decor` |
| `product_type` | String(80) | 产品类型，如 `t-shirt`、`mug` |
| `audience` | String(240) | 目标受众，如 `dog moms` |
| `season` | String(80) | 季节/节日，如 `Mother's Day` |
| `offer` | String(240) | 优惠信息 |
| `destination_url` | String(1024) | 目标链接 |
| `status` | String(40) | 状态：`draft` / `active` / `paused` / `ended` |
| `start_at` / `end_at` | DateTime | 活动起止时间 |
| `created_at` / `updated_at` | DateTime | 创建/更新时间 |

### 2.4 publish_job — 发布任务

每次发帖的内容和数据记录，是整个系统的核心业务表。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Integer | 主键（自增） |
| `job_id` | String(64) | **任务唯一标识**，如 `job_demo_bf1e51db` |
| `account_id` | String(64) | 所属账号 |
| `campaign_id` | String(64) | 所属活动（可空） |
| `status` | String(40) | 状态：`pending` / `running` / `completed` / `failed` / `cancelled` |
| `board_name` | String(160) | Pinterest 画板名称 |
| `image_path` | String(1024) | 图片文件路径 |
| `title` | String(160) | Pin 标题（最长 100 字符） |
| `description` | Text | Pin 描述（最长 800 字符） |
| `destination_url` | String(1024) | 目标链接 |
| `product_type` | String(80) | 产品类型 |
| `niche` | String(120) | 利基市场 |
| `audience` | String(240) | 目标受众 |
| `season` | String(80) | 季节/节日 |
| `offer` | String(240) | 优惠信息 |
| `error_message` | Text | 错误信息 |
| `pin_performance_id` | Integer | 关联的 PinPerformance ID |
| `content_hash` | String(64) | 内容指纹（去重用） |
| `title_hash` | String(64) | 标题指纹（去重用） |
| `description_hash` | String(64) | 描述指纹（去重用） |
| `content_batch_id` | String(64) | 批次 ID（A/B 测试分组） |
| `variant_angle` | String(160) | 变体角度（A/B 测试） |
| `tagged_topics` | Text | Pinterest 标签主题（JSON 数组，可选） |
| `started_at` / `finished_at` | DateTime | 开始/完成时间 |
| `created_at` / `updated_at` | DateTime | 创建/更新时间 |

### 2.5 pin_performance — Pin 效果追踪

记录已发布 Pin 的 AI 生成参数与 Pinterest 实际表现数据，供 EvoMap 学习优化。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Integer | 主键（自增） |
| `account_id` | String(64) | 所属账号 |
| `campaign_id` | String(64) | 所属活动 |
| `pinterest_pin_id` | String(128) | Pinterest 官方 Pin ID |
| `board_id` | String(128) | Pinterest Board ID |
| `product_type` / `niche` | String | 产品类型 / 利基 |
| `title` | String(160) | Pin 标题 |
| `description` | Text | Pin 描述 |
| `destination_url` | String(1024) | 目标链接 |
| `image_url` | String(1024) | 图片 URL |
| `content_prompt` | Text | 生成内容用的 AI prompt |
| `visual_prompt` | Text | 生成图片用的 AI prompt |
| `model_name` | String(160) | 使用的 AI 模型 |
| `prompt_version` | String(40) | Prompt 版本，默认 `v1` |
| `keywords` | JSONB | 关键词列表 |
| `strategy_snapshot` | JSONB | 策略快照 |
| `impressions` | Integer | 展示量（默认 0） |
| `saves` | Integer | 收藏量 |
| `clicks` | Integer | 点击量 |
| `outbound_clicks` | Integer | 外链点击量 |
| `comments` | Integer | 评论量 |
| `reactions` | Integer | 反应量 |
| `ctr` | Float | 点击率 = clicks / impressions |
| `save_rate` | Float | 收藏率 = saves / impressions |
| `engagement_rate` | Float | 互动率 = (saves+comments+reactions) / impressions |
| `published_at` | DateTime | 发布时间 |
| `metrics_updated_at` | DateTime | 数据最后同步时间 |
| `content_hash` / `title_hash` / `description_hash` | String(64) | 内容指纹（去重） |
| `content_batch_id` | String(64) | 批次 ID |
| `variant_angle` | String(160) | 变体角度 |
| `created_at` / `updated_at` | DateTime | 创建/更新时间 |

### 2.6 global_strategy — 全局策略

存储按作用域命名的策略配置和趋势数据。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Integer | 主键（自增） |
| `scope` | String(120) | **唯一**，策略作用域名称 |
| `strategy` | JSONB | 策略内容（JSON） |
| `version` | String(40) | 版本号 |
| `created_at` / `updated_at` | DateTime | 创建/更新时间 |

### 2.7 scheduled_task — 统一任务队列

**核心调度表。** 所有异步任务（发帖、暖机、生成图片、回复评论等）都是这张表的一行。调度器扫描 `pending`/`ready` 状态的行并发送给 Celery 执行。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Integer | 主键（自增） |
| `task_id` | String(64) | **任务唯一标识**，如 `st_demo_c7f6338a` |
| `task_type` | String(40) | 任务类型（见 [第 3 节](#3-任务类型说明)） |
| `platform` | String(40) | 平台，默认 `pinterest` |
| `account_id` | String(64) | 所属账号（可空） |
| `campaign_id` | String(64) | 所属活动（可空） |
| `status` | String(40) | 状态：`pending` / `ready` / `running` / `completed` / `failed` / `cancelled` |
| `priority` | Integer | 优先级，数字越大越优先，默认 0 |
| `scheduled_at` | DateTime | 计划执行时间 |
| `started_at` | DateTime | 实际开始时间 |
| `finished_at` | DateTime | 完成时间 |
| `attempt_count` | Integer | 已尝试次数 |
| `max_attempts` | Integer | 最大尝试次数，默认 3 |
| `next_retry_at` | DateTime | 下次重试时间 |
| `locked_by` | String(64) | 锁定此任务的 worker ID |
| `lock_until` | DateTime | 锁定过期时间 |
| `heartbeat_at` | DateTime | 最后心跳时间 |
| `celery_task_id` | String(128) | Celery 异步任务 ID |
| `payload_json` | JSONB | 任务参数（JSON） |
| `result_json` | JSONB | 任务结果（JSON） |
| `error_message` | Text | 错误信息 |
| `error_type` | String(40) | 错误类型 |
| `created_at` / `updated_at` | DateTime | 创建/更新时间 |

**索引：**
- `ix_st_status_scheduled` on `(status, scheduled_at)` — 调度器扫描用
- `ix_st_account_status` on `(account_id, status)` — 按账号查询
- `ix_st_locked_by` on `(locked_by)` — 查找僵尸任务

### 2.8 reply_record — 评论回复记录

记录 Pinterest 评论及 AI 生成的回复，包含安全审核字段。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Integer | 主键（自增） |
| `account_id` | String(64) | 所属账号 |
| `comment_id` | String(120) | 评论 ID |
| `pin_url` | String(1024) | Pin 链接 |
| `author_name` | String(240) | 评论者名称 |
| `comment_text` | Text | 评论文本 |
| `reply_text` | Text | 生成的回复文本 |
| `status` | String(40) | 状态：`suggested` / `approved` / `posted` / `rejected` |
| `safety_status` | String(40) | 安全审核：`safe` / `unsafe` |
| `safety_reason` | Text | 安全审核原因 |
| `posted_at` | DateTime | 回复发布时间 |
| `raw_json` | JSONB | 原始 API 响应 |
| `created_at` / `updated_at` | DateTime | 创建/更新时间 |

**唯一约束：** `(account_id, comment_id)` — 同一评论不会重复回复。

### 2.9 token_usage — Token 用量追踪

记录每次 LLM API 调用的 token 消耗和费用估算。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Integer | 主键（自增） |
| `provider` | String(40) | 提供商，如 `deepseek`、`volcengine` |
| `model_name` | String(160) | 模型名称 |
| `account_id` | String(64) | 所属账号 |
| `campaign_id` | String(64) | 所属活动 |
| `prompt_tokens` | Integer | 输入 token 数 |
| `completion_tokens` | Integer | 输出 token 数 |
| `total_tokens` | Integer | 总 token 数 |
| `cost_estimate` | Float | 费用估算 |
| `request_type` | String(80) | 请求类型，默认 `chat` |
| `request_id` | String(120) | 请求 ID |
| `created_at` | DateTime | 创建时间 |

---

## 3. 任务类型说明

| task_type | 说明 | payload_json 参数 | Celery 队列 |
|---|---|---|---|
| `publish` | 直接发布 Pin（不暖机） | `job_id`, `dry_run` | publish |
| `warmup_and_publish` | **推荐** 暖机 + 发布（一个浏览器会话） | `account_id`, `job_id`, `warmup_duration_minutes`, `dry_run` | publish |
| `warmup` | 仅暖机浏览，不发布 | `account_id`, `duration_minutes` | publish |
| `generate_image` | 生成图片素材 | `prompt`, `image_size` | media |
| `generate_video` | 生成营销视频 | `prompt`, `image_url`, `duration_seconds`, `aspect_ratio` | media |
| `auto_reply` | 自动回复评论 | `account_id`, `dry_run`, `limit` | engagement |
| `refresh_trends` | 刷新 Pinterest 趋势 | `scope`, `trend_type`, `query`, `niche`, `product_type` | trend |
| `cleanup` | 清理过期素材文件 | `retention_days` | trend |
| `reclaim_stale` | 回收卡住的僵尸任务 | `stale_minutes` | trend |

---

## 4. 通过 nanobot 指令操作

启动 nanobot：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/nanobot.ps1 "你的指令"
```

### 4.1 创建并执行发帖任务

```
用 demo_setup 创建一个发帖测试任务并立刻执行
```

或者手动指定：

```
为账号 test-account-1 的发布任务 job_demo_bf1e51db 创建一个暖机加发布任务，暖机 5 分钟，立即执行
```

### 4.2 查询状态

```
查看所有任务状态
```

```
查看最近 4 小时的失败任务
```

```
查看账号 test-account-1 的运行时状态
```

```
查看任务 st_demo_c7f6338a 的详情
```

### 4.3 调度管理

```
列出所有待处理的任务
```

```
自动为所有有 ready 任务的账户调度今天的发帖计划
```

```
立即执行所有待处理任务（dry run 模式先预览）
```

### 4.4 健康检查

```
检查系统健康状态（数据库、Redis、AdsPower）
```

```
检查所有账户的代理状态
```

### 4.5 清理

```
清除所有待处理、失败和已取消的任务
```

```
取消任务 st_demo_c7f6338a
```

### 4.6 MCP 工具对照

nanobot 会把自然语言映射到以下 MCP 工具：

| 常用操作 | MCP 工具 | 关键参数 |
|---|---|---|
| 创建任务 | `create_publish_task` | account_id, job_id, warmup_duration_minutes |
| 批量调度 | `auto_schedule_daily` | account_ids, force |
| 手动派发 | `dispatch_ready_tasks` | limit, dry_run |
| 查看状态 | `get_task_status` / `get_status_dashboard` | task_id |
| 列出任务 | `list_tasks` | account_id, status, task_type, limit |
| 查看错误 | `get_recent_errors` | hours, limit |
| 健康检查 | `check_health` | — |
| 检查代理 | `check_account_proxies` | account_ids |
| 直接发布 | `publish_pin_direct` | account_id, job_id（仅 dry_run） |
| 生成图片 | `generate_image` | prompt, image_size |
| 刷新趋势 | `refresh_pinterest_trends` | scope, trend_type |

---

## 5. 通过数据库直接操作

> 使用 DBeaver 或 psql 连接 `postgresql://postgres:123456@localhost:5432/pinterest_pod`

### 5.1 创建发布任务

```sql
-- 步骤 1：创建 PublishJob
INSERT INTO publish_job (
    job_id, account_id, campaign_id, status,
    board_name, image_path, title, description,
    product_type, niche, audience, season, offer
) VALUES (
    'job_manual_001',           -- job_id，自定义唯一标识
    'test-account-1',           -- account_id
    NULL,                       -- campaign_id（可选）
    'pending',                  -- 状态
    'My Board',                 -- board_name
    'var/uploads/my_image.png', -- image_path
    'My Pin Title',            -- title
    'My Pin Description',      -- description
    't-shirt',                 -- product_type
    'pet lovers',              -- niche
    'dog moms',                -- audience
    'Summer',                  -- season
    '20% off'                  -- offer
);
```

### 5.2 创建调度任务

```sql
-- 步骤 2：创建 ScheduledTask（关联上面的 job）
INSERT INTO scheduled_task (
    task_id, task_type, platform, account_id,
    status, priority, scheduled_at, max_attempts,
    payload_json
) VALUES (
    'st_manual_001',              -- task_id，自定义唯一标识
    'warmup_and_publish',         -- task_type
    'pinterest',                  -- platform
    'test-account-1',             -- account_id
    'pending',                    -- 状态
    10,                           -- 优先级（越高越先执行）
    NOW(),                        -- 立即执行
    0,                            -- max_attempts=0 表示不重试
    '{
        "account_id": "test-account-1",
        "job_id": "job_manual_001",
        "warmup_duration_minutes": 5,
        "dry_run": false
    }'::jsonb
);
```

### 5.3 查询

```sql
-- 查看所有待处理任务
SELECT task_id, task_type, account_id, status, priority, scheduled_at
FROM scheduled_task
WHERE status IN ('pending', 'ready')
ORDER BY priority DESC, scheduled_at ASC;

-- 查看今日任务统计
SELECT
    status,
    task_type,
    COUNT(*) as cnt
FROM scheduled_task
WHERE created_at >= CURRENT_DATE
GROUP BY status, task_type
ORDER BY status, task_type;

-- 查看最近失败任务
SELECT task_id, task_type, account_id, error_message, error_type, finished_at
FROM scheduled_task
WHERE status = 'failed'
  AND finished_at >= NOW() - INTERVAL '4 hours'
ORDER BY finished_at DESC;

-- 查看某个发布任务的完整信息
SELECT * FROM publish_job WHERE job_id = 'job_manual_001';

-- 查看某个账号的发布历史
SELECT title, published_at, impressions, saves, clicks, ctr
FROM pin_performance
WHERE account_id = 'test-account-1'
ORDER BY published_at DESC
LIMIT 20;
```

### 5.4 取消/删除任务

```sql
-- 取消某个任务（不会被执行）
UPDATE scheduled_task
SET status = 'cancelled', updated_at = NOW()
WHERE task_id = 'st_manual_001';

-- 取消某账号所有待处理任务
UPDATE scheduled_task
SET status = 'cancelled', updated_at = NOW()
WHERE account_id = 'test-account-1'
  AND status IN ('pending', 'ready');

-- 清理所有失败和已取消的任务（慎用）
DELETE FROM scheduled_task
WHERE status IN ('failed', 'cancelled');
```

### 5.5 重置卡住的任务

```sql
-- 将卡在 running 超过 30 分钟的任务重置为 pending
UPDATE scheduled_task
SET status = 'pending',
    locked_by = NULL,
    lock_until = NULL,
    updated_at = NOW()
WHERE status = 'running'
  AND started_at < NOW() - INTERVAL '30 minutes';
```

### 5.6 修改账户策略

```sql
-- 调整每日发帖上限
UPDATE account_policy
SET daily_max_posts = 5, updated_at = NOW()
WHERE account_id = 'test-account-1';

-- 临时冻结账户（冷却到明天）
UPDATE account_policy
SET cooldown_until = NOW() + INTERVAL '1 day', updated_at = NOW()
WHERE account_id = 'test-account-1';
```

---

## 6. 服务启动与停止

### 启动所有服务

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_all.ps1
```

启动前会自动检查并停止旧的 Celery/FastAPI 进程。

如果不想停旧进程：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_all.ps1 -NoStopExisting
```

### 停止所有服务

```powershell
powershell -ExecutionPolicy Bypass -File scripts/stop_all.ps1
```

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SCHEDULER_ENABLED` | `true` | 是否启用调度器 |
| `SCHEDULER_DRY_RUN` | `false` | 是否干跑模式（不实际发帖） |
| `SCHEDULER_AUTO_DISPATCH_ENABLED` | `false` | 是否自动派发任务（建议保持 false，手动派发） |

### 单独启动组件

```powershell
# FastAPI 服务
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8900

# Celery Worker（四队列）
..\.venv\Scripts\celery.exe -A app.celery_app worker -Q publish,media,engagement,trend --loglevel=info --concurrency=1 --pool=solo

# Celery Beat（定时调度）
..\.venv\Scripts\celery.exe -A app.celery_app beat --loglevel=info
```

---

## 7. 常见问题排查

### 7.1 任务一直 pending 不执行

**原因：** 没有触发 dispatch。

**解决：**
```
立即执行所有待处理任务
```

或者检查 Beat 是否启用了自动派发：
```powershell
$env:SCHEDULER_AUTO_DISPATCH_ENABLED = "true"
```

### 7.2 任务卡在 running

**原因：** 任务执行中崩溃，状态未写回。

**解决：** 数据库直接重置（见 [5.5](#55-重置卡住的任务)），或等待 reclaim_stale 任务（每 10 分钟自动回收超过 45 分钟的僵尸任务）。

### 7.3 代理连接失败

**症状：** 日志显示 `net::ERR_SOCKS_CONNECTION_FAILED`

**解决：** 检查 AdsPower profile 的代理是否有效。用 nanobot 检查：
```
检查所有账户的代理状态
```

### 7.4 Redis 锁未释放

**症状：** 新任务提示无法获取锁。

**解决：** 清除所有 Redis 锁：
```powershell
..\.venv\Scripts\python.exe -c "import redis; r=redis.from_url('redis://localhost:6379'); [r.delete(k) for k in r.keys('nanobot:lock:*')]; print('Locks cleared')"
```

### 7.5 发布失败查看截图

失败的 debug 截图在 `var/debug/pinterest/` 目录下，按时间戳命名。包含：
- `page.png` — 失败时的页面截图
- `page.html` — 页面 HTML
- `url.txt` — 当前 URL

### 7.6 查看 Celery Worker 日志

日志文件位置：
```
var/log/worker.log          # 最近一次 worker 日志
var/logs/celery-worker.out.log
var/logs/celery-worker.err.log
```

### 7.7 强制停止所有进程

```powershell
powershell -ExecutionPolicy Bypass -File scripts/stop_all.ps1
```

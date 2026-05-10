# Pinterest Pod Agent — 部署指南

> 适用版本：pinterest-pod-agent，更新时间：2026-05-10

## 目录

1. [系统要求](#1-系统要求)
2. [依赖安装](#2-依赖安装)
3. [环境变量配置](#3-环境变量配置)
4. [数据库初始化](#4-数据库初始化)
5. [启动服务](#5-启动服务)
6. [验证部署](#6-验证部署)
7. [停止服务](#7-停止服务)
8. [常见问题](#8-常见问题)

---

## 1. 系统要求

### 硬件

| 配置 | 最低 | 推荐 |
|---|---|---|
| CPU | 2 核 | 4 核+ |
| 内存 | 4 GB | 8 GB+ |
| 磁盘 | 10 GB 可用 | 50 GB+ SSD |

> 每个并发浏览器约占用 500MB-1GB 内存。并发数高时按比例加内存。

### 软件

| 软件 | 版本要求 | 用途 |
|---|---|---|
| **Windows** | 10 / 11 或 Windows Server 2019+ | 操作系统（AdsPower 仅支持 Windows/macOS） |
| **Python** | 3.11+（推荐 3.13） | 运行后端和脚本 |
| **PostgreSQL** | 14+ | 持久化所有数据 |
| **Redis** | 6+（推荐 7） | 消息队列和分布式锁 |
| **AdsPower** | 最新版 | 指纹浏览器，管理 Pinterest 账号 Profile |
| **Git** | 任意 | 拉取代码 |

### 外部服务账号

| 服务 | 用途 | 获取方式 |
|---|---|---|
| **Volcengine（火山引擎）** | LLM 生成标题、描述、回复 | [console.volcengine.com](https://console.volcengine.com) |
| **Fal.ai** | AI 图片生成 | [fal.ai/dashboard](https://fal.ai/dashboard) |
| **AdsPower Local API** | 控制浏览器自动化 | AdsPower 桌面端 → 设置 → 本地 API |

---

## 2. 依赖安装

### 2.1 安装 Python

从 [python.org](https://www.python.org/downloads/) 下载安装 Python 3.11+。

验证：
```powershell
python --version
```

### 2.2 安装 PostgreSQL

Windows 推荐从 [postgresql.org](https://www.postgresql.org/download/windows/) 下载安装。

安装时记住 superuser 密码（本文档假设 `postgres` / `123456`）。

验证：
```powershell
psql -U postgres -c "SELECT version();"
```

### 2.3 安装 Redis

**方式一：Docker（推荐）**

```powershell
docker run -d --name nanobot-redis -p 6379:6379 --restart unless-stopped redis:7
```

**方式二：Windows 原生**

从 [redis.io](https://redis.io/downloads/) 或 GitHub Releases 下载 Windows 版 Redis，或使用 `winget`：

```powershell
winget install Redis
```

验证：
```powershell
python -c "import redis; r=redis.from_url('redis://localhost:6379/0'); print(r.ping())"
```
输出 `True` 表示正常。

### 2.4 安装 AdsPower

从 [adspower.com](https://www.adspower.com/) 下载安装桌面版。

安装后：
1. 打开 AdsPower → 设置 → 本地 API → 启用
2. 记下 API 端口（默认 50325）
3. 创建或导入浏览器 Profile，记下 Profile ID（如 `k1buvn6c`）

### 2.5 克隆项目

```powershell
git clone https://github.com/jia137361-dotcom/Fei.git
cd Fei\pinterest-pod-agent
```

如果项目已经 clone 在其他目录，需要修改 `scripts/start_all.ps1` 和 `scripts/stop_all.ps1` 中的 `$ProjectRoot` 和 `$VenvRoot` 路径。

### 2.6 创建虚拟环境并安装依赖

```powershell
# 在 pinterest-pod-agent 上级目录创建 venv（与启动脚本路径一致）
cd ..
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\pip.exe install -r pinterest-pod-agent\requirements.txt

# 安装 Playwright 浏览器
.\.venv\Scripts\playwright.exe install chromium
```

---

## 3. 环境变量配置

将 `pinterest-pod-agent\.env.example` 复制为 `.env` 并填写：

```powershell
copy .env.example .env
```

### 必填项

```env
# 数据库连接
DATABASE_URL=postgresql+psycopg://postgres:你的密码@localhost:5432/pinterest_pod

# Redis 连接
REDIS_URL=redis://localhost:6379
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1

# API 鉴权（用来保护 FastAPI 端点，nanobot 通过 MCP 工具连接时使用）
API_KEY=你的自定义密钥

# 火山引擎 LLM（生成标题/描述/回复）
VOLC_API_KEY=你的火山API密钥
VOLC_MODEL=ark-4136acc2-b228-4b02-8bc9-e46e3d3030a6-6430e
VOLC_BASE_URL=https://ark.cn-beijing.volces.com/api/v3

# AdsPower 浏览器
ADSPOWER_BASE_URL=http://local.adspower.net:50325
ADSPOWER_API_KEY=你的AdsPower API密钥
```

### 可选

```env
# Fal.ai 图片生成
FAL_KEY=你的fal.ai密钥

# 调度器控制
SCHEDULER_ENABLED=true
SCHEDULER_DRY_RUN=false
SCHEDULER_AUTO_DISPATCH_ENABLED=false

# 暖机行为
WARMUP_ENABLE_PIN_ENGAGEMENT=false
WARMUP_ENABLE_SAVE=false
```

### 环境变量说明

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SCHEDULER_ENABLED` | `true` | 是否启用 Celery Beat 调度器 |
| `SCHEDULER_DRY_RUN` | `true` | **生产环境必须设为 `false`**，否则不会真正发帖 |
| `SCHEDULER_AUTO_DISPATCH_ENABLED` | `false` | `true` 时调度器自动派发到期任务；建议保持 `false`，通过 nanobot 手动触发 |
| `SCHEDULER_TIMEZONE` | `Asia/Shanghai` | 调度器时区 |
| `PUBLISH_INTERVAL_MINUTES` | `30` | 调度扫描间隔（分钟） |

---

## 4. 数据库初始化

### 4.1 创建数据库

```powershell
psql -U postgres -c "CREATE DATABASE pinterest_pod;"
```

如果数据库已存在想重建（会丢弃所有数据）：

```powershell
psql -U postgres -c "DROP DATABASE IF EXISTS pinterest_pod; CREATE DATABASE pinterest_pod;"
```

### 4.2 运行数据库迁移

```powershell
cd pinterest-pod-agent
..\..\.venv\Scripts\alembic.exe upgrade head
```

> **注意**：路径取决于你的 venv 位置。如果 venv 在 `pinterest-pod-agent` 的上级目录，使用 `..\.venv\Scripts\alembic.exe`。

### 4.3 创建最小数据（可选）

迁移完成后，数据库表已建好。要让系统跑起来，需要至少：
- 1 条 `social_account` 记录
- 1 条 `account_policy` 记录
- 1 条 `content_template` 记录（或全局模板）

可以通过 nanobot 指令创建账号和模板，或直接写 SQL 插入。详见 [数据库操作手册](var/docs/数据库操作手册.md)。

---

## 5. 启动服务

### 一键启动（推荐）

```powershell
# 确保在 pinterest-pod-agent 目录下
powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1
```

这会依次启动：
1. **FastAPI**（端口 8900）— REST API + MCP Server
2. **Celery Worker**（4 队列：publish, media, engagement, trend）
3. **Celery Beat** — 定时调度 + 僵尸任务回收

### 手动分别启动

适合调试场景，每个组件开一个终端窗口：

```powershell
# 终端 1：FastAPI
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8900

# 终端 2：Celery Worker
..\.venv\Scripts\celery.exe -A app.celery_app worker -Q publish,media,engagement,trend --loglevel=info --concurrency=2 --pool=solo

# 终端 3：Celery Beat
..\.venv\Scripts\celery.exe -A app.celery_app beat --loglevel=info
```

### 关于 concurrency

Celery Worker 的 `--concurrency` 参数控制同时打开几个浏览器。`--pool=solo` 是 Windows 下的要求。

| 服务器配置 | 建议 concurrency |
|---|---|
| 2 核 4GB | 1-2 |
| 4 核 8GB | 3-5 |
| 8 核 16GB | 8-12 |

---

## 6. 验证部署

### 6.1 检查三组件运行状态

```powershell
# FastAPI
Invoke-RestMethod http://127.0.0.1:8900/health

# Celery Worker
..\.venv\Scripts\celery.exe -A app.celery_app inspect ping --timeout=5

# Celery Beat（查看日志窗口或检查调度记录）
```

### 6.2 用 nanobot 全面检查

```powershell
powershell -ExecutionPolicy Bypass -File scripts\nanobot.ps1 "检查服务健康状态"
```

输出应显示：
- 数据库：连接正常
- Redis：连接正常
- AdsPower：连接正常
- Celery Worker：在线

### 6.3 干跑测试

在 `.env` 中设置 `SCHEDULER_DRY_RUN=true`，然后执行一个测试发布任务：

```
用 demo_setup 创建一个发帖测试任务并立刻执行
```

干跑模式会走完整流程但不会真正点击发布按钮。确认无错误后，将 `SCHEDULER_DRY_RUN=false` 重启服务。

### 6.4 检查代理

```powershell
powershell -ExecutionPolicy Bypass -File scripts\nanobot.ps1 "检查所有账户的代理状态"
```

---

## 7. 停止服务

```powershell
powershell -ExecutionPolicy Bypass -File scripts\stop_all.ps1
```

停止所有 Celery 进程和 FastAPI 进程。

如果通过 `-NoStopExisting` 启动了额外的实例，需要手动检查：

```powershell
Get-Process -Name celery -ErrorAction SilentlyContinue | Select-Object Id, StartTime
Get-NetTCPConnection -LocalPort 8900 -ErrorAction SilentlyContinue
```

---

## 8. 常见问题

### 8.1 任务一直 pending 不执行

`SCHEDULER_AUTO_DISPATCH_ENABLED=false` 时（默认），调度器不会自动派发任务。需要通过 nanobot 手动触发：

```
立即执行所有待处理任务
```

### 8.2 浏览器打不开 / 发帖失败

排查顺序：
1. AdsPower 客户端是否打开、本地 API 是否启用
2. `.env` 中 `ADSPOWER_BASE_URL` 是否正确
3. 账号的 `adspower_profile_id` 是否对应 AdsPower 中存在的 Profile
4. `SCHEDULER_DRY_RUN` 是否误设为 `true`
5. 代理是否有效（日志中出现 `ERR_SOCKS_CONNECTION_FAILED` 说明代理不通）

### 8.3 Celery Worker 无法启动 / inspect ping 无响应

```powershell
# 确认 Redis 正常
..\.venv\Scripts\python.exe -c "import redis; print(redis.from_url('redis://localhost:6379/0').ping())"

# 确认没有脑裂 worker（Celery 进程有独立的 node name）
..\.venv\Scripts\celery.exe -A app.celery_app inspect active --timeout=5
```

### 8.4 启动后自动跑任务

检查：
1. `SCHEDULER_AUTO_DISPATCH_ENABLED` 是否为 `true`（设为 `false` 停止自动派发）
2. Redis 队列中是否有旧任务残留
3. 是否有旧 Worker 进程未停干净

### 8.5 任务卡在 running 超过 30 分钟

```sql
UPDATE scheduled_task
SET status = 'pending', locked_by = NULL, lock_until = NULL, updated_at = NOW()
WHERE status = 'running' AND started_at < NOW() - INTERVAL '30 minutes';
```

或等待 `reclaim_stale` 定时任务自动回收（默认每 10 分钟回收超过 45 分钟的僵尸任务）。

### 8.6 Redis 锁未释放

```powershell
..\.venv\Scripts\python.exe -c "import redis; r=redis.from_url('redis://localhost:6379'); [r.delete(k) for k in r.keys('nanobot:lock:*')]; print('ok')"
```

### 8.7 路径问题

启动脚本 `start_all.ps1` 和 `stop_all.ps1` 默认假设：
- 项目目录：`c:\nanobot\pinterest-pod-agent`
- 虚拟环境：`c:\nanobot\.venv`

如果你的路径不同，修改这两个脚本开头的 `$ProjectRoot` 和 `$VenvRoot` 变量。

### 8.8 端口冲突

FastAPI 默认 8900 端口。如需修改：
- 修改 `start_all.ps1` 中的 `--port 8900`
- 同步修改 `stop_all.ps1` 中检查的端口号
- 同步修改 nanobot 连接的目标端口

### 8.9 查看日志

| 日志来源 | 位置 |
|---|---|
| Celery Worker stdout | `var/log/worker.log` |
| Celery Worker 启动日志 | `var/logs/celery-worker.out.log` |
| Celery Worker 错误 | `var/logs/celery-worker.err.log` |
| 发布失败截图 | `var/debug/pinterest/`（含 page.png, page.html, url.txt） |

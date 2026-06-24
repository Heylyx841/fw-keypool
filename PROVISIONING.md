# fw-keypool 操作手册

> 风险声明见 [`RISK_NOTICE.md`](RISK_NOTICE.md)。自动化批量注册违反 Fireworks ToS，仅供学习研究。

## 一、环境准备

### 1.1 系统依赖
- Python 3.11+
- Git

### 1.2 安装 Python 依赖
```bash
cd fw-keypool/registrar
pip install -e .
playwright install chromium
```
orchestrator / pool-gateway 依赖 `httpx`、`python-dotenv`（registrar 已含）。

---

## 二、邮箱池填写模板说明

### 2.1 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `email` | 是 | 注册用邮箱地址 |
| `protocol` | 是 | `pop3` 或 `imap` |
| `host` | 否 | 收信服务器主机，留空按邮箱域名自动匹配（Gmail/QQ/163/Outlook 等已内置） |
| `port` | 否 | 端口，留空自动匹配（POP3 995 / IMAP 993） |
| `username` | 否 | 登录用户名，留空默认用 email |
| `auth_code` | 是 | **授权码/应用专用密码**（非登录密码！QQ/163/Gmail 均需在邮箱设置开启并生成） |
| `alias_pattern` | 否 | Catch-all 域名邮箱的别名模板，含 `{seq:N}` 占位符 |
| `note` | 否 | 备注 |

### 2.2 常见邮箱授权码获取
- **QQ 邮箱**：设置 → 账户 → POP3/SMTP 服务 → 开启 → 生成授权码
- **163 邮箱**：设置 → POP3/SMTP/IMAP → 开启 → 客户端授权码
- **Gmail**：账户 → 安全 → 两步验证 → 应用专用密码
- **Outlook**：账户安全 → 应用密码

### 2.3 Catch-all 域名邮箱（批量别名）
若你有自建域名并配置了 Catch-all（如 Cloudflare Email Routing 转发到同一信箱），可批量生成别名地址：
```csv
email,protocol,host,port,username,auth_code,alias_pattern
base@mydomain.com,imap,imap.mydomain.com,993,base@mydomain.com,AUTH_CODE,base+fw{seq:03d}@mydomain.com
```
运行时 `--alias-count 20` 会展开为 `base+fw000@mydomain.com` ~ `base+fw019@mydomain.com`，验证邮件都回到 `base@mydomain.com` 收件箱。

### 2.4 创建邮箱池
```bash
cp emails.example.csv emails.csv
# 编辑 emails.csv 填入实际邮箱
```

---

## 三、配置环境变量
```bash
cp .env.example .env
# 编辑 .env（NEWAPI_ADMIN_PASS、HEADLESS、REGISTRAR_CONCURRENCY 等）
```

`registrar/config.yaml` 默认配置（已抓包固化）：
- `proxy_pool`：默认本机代理 `http://127.0.0.1:3067`（降低封 IP 风险，按需改）
- `headless: true`：不弹出浏览器窗口（无人值守），headless 间歇降级已由降级页检测 + max_page_retry=3 重试兜底；如需观察流程可设 false 弹窗
- `enable_stealth: false`：stealth 设 false 反触发降级，仅用 `add_init_script` 删 webdriver

---

## 四、启动 New API 号池

### 4.1 部署方式（二选一）

**方式 A：Windows 二进制（无需 Docker，推荐本机自用）**
```bash
cd pool-gateway
# 下载 new-api-v1.0.0-rc.14.exe 改名 new-api.exe 放到此目录
# https://github.com/quantumnous/new-api/releases/download/v1.0.0-rc.14/new-api-v1.0.0-rc.14.exe
# 启动（SQLite 模式，不设 SQL_DSN 环境变量）：
$env:REGISTER_ENABLED='false'; $env:TZ='Asia/Shanghai'; .\new-api.exe
```

**方式 B：Docker（备选，未测试）**
```bash
cd pool-gateway
docker compose up -d
```
> ⚠️ Docker 方式未经实测，如遇问题请优先使用 Windows 二进制方式 A。

### 4.2 初始化 + 生成 API Token

New API v1.0.0-rc.14 新版流程（实测）：
```bash
# 1. 初始化 root 账号（POST /api/setup，非环境变量自动创建）
curl -X POST http://127.0.0.1:3000/api/setup \
  -H "Content-Type: application/json" \
  -d '{"username":"root","password":"changeme123","confirmPassword":"changeme123","SelfUseModeEnabled":true,"DemoSiteEnabled":false}'

# 2. 填 newapi.env（NEWAPI_ADMIN_USER/PASS 用于 sync_channels login 拿 session）
cp newapi.env.example newapi.env

# 3. sync_channels.py 自动 login + 录入渠道（无需手动生成 token）
# /v1/chat/completions 用的 API token 需在 New API web「令牌」页创建（unlimited_quota）
```

访问 http://127.0.0.1:3000，用 root / changeme123 登录，在「令牌」页创建 API token（/v1 调用用）。

---

## 五、Fireworks 注册流程（已抓包固化）

完整流程已固化到 [`registrar/config.yaml`](registrar/config.yaml)，无需现场抓包：

1. 注册(2步)：邮箱页 → 密码页（`form.requestSubmit()` 触发 React onSubmit）
2. 验证邮件：`no-reply@fireworks.ai` / `Verify your Fireworks account` / 验证链接
3. 登录：`login-form-email/password/submit`（data-testid 定位）
4. onboarding(2页)：firstName+lastName+Terms+Continue → 问卷2选项+Submit to get $6
5. API Key：`Create API Key` → `API Key` 菜单 → 命名 → `Generate Key` → `<code>fw_xxx</code>`

反爬与可靠性机制：
- React 受控 input 用 `_fill_react_input`（nativeInputValueSetter + 重置 `_valueTracker`），普通 `page.fill` 不可靠会导致 Fireworks 端密码≠本地（登录 Invalid 根因）
- 降级页检测：无 React `_valueTracker` 的降级 HTML 页自动等待+重试
- onboarding 防卡死：Continue disabled 检测 + 第2页未出现则刷新重试（最多3次）
- Submit to get：点击后等待 URL 跳转，未跳转则重复点击最多5次
- 每次刷新重试重新生成随机 firstName/lastName（避免风控标记）

若运行中发现选择器失效（Fireworks 改版），再用浏览器开发者工具抓包更新 `config.yaml` 的 `form_selectors`。

---

## 六、运行造号

### 6.1 单独运行造号端
```bash
cd registrar
# 仅加载邮箱池入库（不运行）
python run.py --load-only
# 运行（全量）
python run.py
# 运行（限量 5 个测试）
python run.py --limit 5
# Catch-all 别名展开
python run.py --alias-count 20
# 重置失败任务重试
python run.py --reset-failed
# 仅导出 keys.json
python run.py --export-keys
```

### 6.1 登录测试（从登录开始，跳过注册）
账号已注册但需单独复现登录/onboarding 链路时用。密码从 DB 读（明文），走代理：
```bash
cd registrar
python login_test.py --list                       # 列出 DB 中带密码+授权码的账号（明文）
python login_test.py --email user@example.com     # 指定邮箱登录测试
python login_test.py --email xxx --password 'xxx' # 手动指定密码
python login_test.py -v                           # 详细日志
```

### 6.2 同步 Key 到号池
```bash
cd pool-gateway
python sync_channels.py
```

### 6.3 一键流水线（造号 + 同步 + 巡检 + 告警）
```bash
cd orchestrator
python run_pipeline.py --limit 5
# 跳过某步
python run_pipeline.py --skip-register --skip-health
```

### 6.4 健康巡检（持续）
```bash
cd orchestrator
python health_check.py            # 持续巡检（间隔 600s）
python health_check.py --once     # 单次
```

---

## 七、使用号池（两种入口）

### 7.0 进入 New API 后台管理

New API 启动后（`python start.py` 自动启动，或手动运行 `pool-gateway/new-api.exe`），用浏览器访问后台：
- 地址：http://127.0.0.1:3000
- 账号：`root` / 密码：`changeme123`（首次由 `start.py` 的 `POST /api/setup` 自动初始化；手动初始化见 4.2）
- 后台管理功能：
  - **渠道**（`/console/channel`）：查看/测试/禁用/删除 Fireworks 渠道（`sync_channels.py` 自动录入，命名 `fw-<email>`，对应 `data/keys.json` 每个 key）
  - **令牌**（`/console/token`）：创建/查看 `/v1` 调用用的 API token（`sk-` 前缀，`unlimited_quota`）
  - **日志**：请求记录 / 消费额度 / 错误详情
  - **设置**：系统配置 / 模型重定向 / 分组 / 速率限制
- 状态检查：`GET http://127.0.0.1:3000/api/status`（healthcheck 用）

### 7.1 入口 A：New API 统一入口（加权随机轮询）

客户端对接 New API 统一入口（OpenAI 兼容），用 New API「令牌」页创建的 API token：
```bash
curl http://127.0.0.1:3000/v1/chat/completions \
  -H "Authorization: Bearer sk-<New API 令牌>" \
  -H "Content-Type: application/json" \
  -H "X-Custom-Header: anything" \
  -d '{
    "model": "accounts/fireworks/models/glm-5p2",
    "messages": [{"role": "user", "content": "hello"}]
  }'
```
New API 自动在多个 Fireworks 渠道间加权随机轮询 + 失败重试。

**自定义标头转发**（确保所有 Fireworks 接口能正常转发）：
- 客户端请求自带的任意标头（如 `X-Custom-Header`、`X-Fireworks-*`）：New API 默认透传到上游（`Authorization` 被渠道 key 替换，hop-by-hop 头除外）
- 渠道级固定标头：在 `pool-gateway/newapi.env` 设 `FIREWORKS_CHANNEL_HEADERS`（JSON 字符串），`sync_channels.py` 录入渠道时写入 `channel.header` 字段，New API 转发时附加到上游 Fireworks。例：`FIREWORKS_CHANNEL_HEADERS={"X-Fireworks-Gen-Random-Seed":"42"}`

### 7.2 入口 B：sticky 转发代理（同 key 优先 + 连续失败 N 次换 key）

Fireworks 有 token 缓存（prefix/KV cache），同一对话用同一 key 命中缓存更快、更省。
New API 原生是加权随机轮询（每次换 key），无法保证粘性。`sticky_proxy.py` 提供第二个入口：
```bash
cd pool-gateway
python sticky_proxy.py                          # 默认 127.0.0.1:3001, 连续失败 3 次换 key
python sticky_proxy.py --fail-threshold 5 -v    # 连续失败 5 次换 key + 详细日志
```
调用（无需 New API token，代理自动用 Fireworks key，透传所有标头/body）：
```bash
curl http://127.0.0.1:3001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Custom-Header: anything" \
  -d '{"model":"accounts/fireworks/models/glm-5p2","messages":[{"role":"user","content":"hi"}]}'
```
状态查询（当前 sticky key / 连续失败计数 / 切换历史）：
```bash
curl http://127.0.0.1:3001/sticky/status
```

sticky 策略（**连续失败**语义）：
- 正常请求始终用**同一个 key**（命中 Fireworks token 缓存）
- 任何一次成功 → 连续失败计数归零
- 失败（429 / 5xx / 连接 / 超时）→ 连续失败计数 +1（429 不特殊处理，统一计数）
- **连续失败 ≥ N 次**（`--fail-threshold` / `STICKY_FAIL_THRESHOLD`，默认 3）才切换下一 key
- 切换后新 key 成为 sticky key，继续优先复用
- 所有 Fireworks 接口（`/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`、`/v1/models` 等）均透传，流式 SSE 逐 chunk 转发

> 两个入口共用 `data/keys.json`（同一批 Fireworks key），按需选择：
> - New API (3000)：随机轮询 + 管理 UI + 健康巡检 + 计费
> - sticky_proxy (3001)：同 key 优先 + token 缓存命中优化 + 连续失败 N 次换 key

> 模型名需用 `accounts/fireworks/models/` 前缀（如 `accounts/fireworks/models/glm-5p2`），已在 `pool-gateway/models.json` 配置。

---

## 八、状态机与断点续跑

任务状态：`pending → registering → email_verifying → verified → fetching_key → done`（失败 `failed`）

- 状态存于 `data/state.db`（SQLite）
- 任意时刻中断，重跑 `python run.py` 自动从上次状态恢复，已完成邮箱不重复注册
- `failed` 任务可用 `--reset-failed` 重置为 `pending` 重试

---

## 九、目录说明

```
fw-keypool/
├── emails.example.csv/.json   # 邮箱池模板（复制后填数据）
├── .env.example               # 环境变量模板
├── registrar/                 # 造号端
│   ├── config.yaml            # ★ P0 抓包固化此文件
│   ├── email_pool.py          # 邮箱池加载校验
│   ├── state_db.py            # SQLite 状态机
│   ├── mail_fetcher.py        # POP3/IMAP 收信
│   ├── verifier_extract.py    # 验证码/链接提取
│   ├── fireworks_registrar.py # Playwright 注册
│   ├── key_fetcher.py         # API Key 申请
│   ├── orchestrator.py        # 造号编排
│   └── run.py                 # CLI 入口
├── pool-gateway/              # 号池承载（New API + sticky 代理）
│   ├── docker-compose.yml     # New API 部署（Docker 备选，未测试）
│   ├── sync_channels.py       # 渠道录入（含自定义标头 channel.header）
│   ├── sticky_proxy.py        # sticky 转发代理（同 key 优先 + 连续失败 N 次换 key）
│   ├── models.json            # Fireworks 模型列表
│   └── newapi.env.example     # New API + sticky 配置
├── orchestrator/              # 运维编排
│   ├── run_pipeline.py        # 一键流水线
│   ├── health_check.py        # 渠道巡检
│   └── alerts.py              # 库存告警
└── data/                      # 运行期数据（gitignore）
    ├── state.db
    └── keys.json
```

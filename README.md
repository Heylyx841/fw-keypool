> # ⚠️ 本项目仅供学习研究；因任何个人或组织不当使用产生的一切后果由使用者自行承担，与原始作者无关。完整条款详见 [`RISK_NOTICE.md`](RISK_NOTICE.md)。

# fw-keypool — Fireworks API 号池自动化

## 项目简介

自动化构建 **Fireworks AI API 号池**：用 POP3/SMTP 接收验证邮件批量注册 Fireworks 账号 → 申请 API Key → 录入 [New API](https://github.com/quantumnous/new-api) 网关，形成可统一调度的多 Key 轮询号池。

## 架构

```
邮箱池 → registrar(Python/Playwright) 造号 → SQLite 状态库
                                               ↓ keys.json
                             sync_channels.py → New API 网关(3000) ── 加权随机轮询+失败重试+计费(已关闭)
                                                   ↓
                                           统一 /v1/chat/completions 入口（无限调用）
                             sticky_proxy.py  → sticky 代理(3001) ── 同 key 优先+连续失败 N 次永久禁用该 key（纯透传无计费）
                                                   ↓
                                           统一 /v1/chat/completions 入口（无限调用）

python start.py 一键同时拉起 New API(3000) + sticky_proxy(3001) 双入口
```

## 目录结构

```
fw-keypool/
├── start.py            # 一键启动（New API + sticky_proxy + 造号 + 同步，无限调用）
├── log_system.py       # 日志系统（控制台输出+保存到文件，最多留3份轮转）
├── registrar/          # 造号端：注册+取Key+登录测试流水线
│   ├── run.py            # 造号 CLI 入口（注册全流程）
│   ├── login_test.py     # 登录测试入口（从登录开始，跳过注册）
│   └── ...               # config/state_db/fireworks_registrar/key_fetcher 等
├── pool-gateway/       # 号池端：New API 部署+渠道录入+sticky 转发代理
│   ├── sync_channels.py    # 录入渠道（含自定义标头 header 字段）
│   └── sticky_proxy.py     # sticky 转发代理（同 key 优先+连续失败 N 次永久禁用该 key）
├── orchestrator/       # 运维：一键流水线+健康巡检
├── data/               # 运行期数据（gitignore）
└── emails.example.csv  # 邮箱池填写模板
```

## 快速开始（一键启动）

最简方式，一条命令完成 New API(3000) + sticky_proxy(3001) 双入口启动 + 邮箱池录入 + 注册造号 + 渠道同步 + 计费关闭（无限调用）：
```bash
python start.py                 # 全流程（New API + sticky_proxy 未运行则自动启动+初始化+关闭计费）
python start.py --limit 1       # 只造 1 个号
python start.py --api-key mykey # 固定生成的 API token key（默认 123456，即 sk-123456）
python start.py --newapi-key sk-xxx   # 指定已有 API token（跳过生成+固定）
python start.py --skip-register       # 跳过造号（仅启动+同步已有 key）
python start.py --skip-sticky         # 跳过 sticky_proxy(3001) 启动
```

输出含 API token + 双入口调用示例 + 号池渠道数。两入口均为**无限调用**（无模型价格/已用/剩余限制）：
- New API(3000)：token `unlimited_quota=True` + 永不过期 + 计费统计已关闭（`configure_unlimited_billing`）
- sticky_proxy(3001)：纯透传，无任何计费/价格/已用/剩余概念
- 仅受上游 Fireworks 账号 credit 限制，号池多 key 轮换即可视作无限

**固定 API token key**（方便记忆/测试）：
- 生成的 New API token key 固定为 `123456`（即调用 token 为 `sk-123456`），可用 `--api-key <值>` 或环境变量 `FW_FIXED_API_KEY` 修改
- 实现：New API AddToken 后端自动生成随机 key，`start.py` 创建后直接 UPDATE `one-api.db` tokens 表把 key 改为固定值（`_fix_token_key`，处理唯一索引冲突）
- 验证：`curl -H "Authorization: Bearer sk-123456" http://127.0.0.1:3000/v1/chat/completions ...` 200 成功

---

## 快速开始（分步）

### 1. 环境准备
- Python 3.11+
- Node.js（Playwright 浏览器下载）

### 2. 安装依赖
```bash
cd fw-keypool/registrar
pip install -e .
playwright install chromium
```

### 3. 填邮箱池
```bash
cp emails.example.csv emails.csv
# 编辑 emails.csv，填入邮箱/协议/host/端口/授权码
```

#### 批量导入 raw.txt（`[邮箱]----[pop授权码]` 格式）
若已有形如 `email----auth_code` 每行一条的原始凭据文件 `raw.txt`，可用转换脚本一键导入（追加、自动去重、按域名匹配收信服务器）：
```bash
python convert_raw.py
# 默认读 fw-keypool/raw.txt → 追加到 fw-keypool/emails.csv，协议 pop3
# 可选参数：--raw <path> --csv <path> --protocol {pop3,imap} --note <备注>
```
- 163/126/qq/gmail 等常见域名自动匹配 POP3/IMAP 默认 host:port，未匹配默认 `pop.<domain>:995`
- 幂等：重复运行只追加新邮箱，已存在的跳过；`raw.txt` 含授权码已 gitignore

### 4. 配置环境
```bash
cp .env.example .env
# 编辑 .env
```

### 5. 启动 New API 号池

**方式 A：Windows 二进制（推荐，无需 Docker）**
```bash
cd pool-gateway
# 下载 new-api Windows 二进制放此目录（见 PROVISIONING.md 4.1）
$env:REGISTER_ENABLED='false'; .\new-api.exe    # SQLite 模式，127.0.0.1:3000
```

**方式 B：Docker（备选，未测试）**
```bash
cd pool-gateway
docker compose up -d
# 访问 http://127.0.0.1:3000 初始化 root 账号
```
> ⚠️ Docker 方式未经实测，如遇问题请优先使用 Windows 二进制方式 A。

### 6. 运行造号流水线
```bash
cd registrar
python run.py                # 全量造号
python run.py --limit 1      # 单个测试
```

#### 假邮箱自动禁用（POP 登录连续失败 >阈值）
注册流程收信阶段若 POP3/IMAP 登录连续失败超过阈值（默认 `>3` 即第 4 次），判定为**假邮箱**并永久禁用，不再重试注册：
- 阈值配置：`config.yaml` → `mail.pop_login_fail_threshold`（默认 3）或环境变量 `MAIL_POP_LOGIN_FAIL_THRESHOLD`
- 判定链：[`mail_fetcher`](registrar/mail_fetcher.py) 识别登录失败抛 `PopLoginError` → 累计计数（DB `pop_fail_count`）超阈值抛 `FakeEmailError` → [`orchestrator`](registrar/orchestrator.py) 捕获后 `set_email_disabled` + 标 `failed`
- 禁用后 [`list_pending`](registrar/state_db.py) 自动跳过 `email_disabled=1` 的邮箱，后续 `run.py` 不再处理
- 与一般网络错误区分：认证失败（`poplib.error_proto`/IMAP login 异常）计假邮箱；连接超时等瞬时错误继续轮询不计
- 手动取消禁用：`StateDB.set_email_disabled(email, False)`

#### 日志系统（控制台输出 + 保存到文件，最多留 3 份）
[`log_system.py`](log_system.py) 在各入口（`run.py` / `start.py` / `sticky_proxy.py` / `sync_channels.py`）启动时自动捕获控制台输出（`print` + `logging` + 异常 traceback）并保存到日志文件：

- 日志文件：`data/logs/run_YYYYMMDD_HHMMSS.log`（UTF-8，含 Windows GBK 终端兼容降级）
- 轮转：每次运行生成一份新日志，自动删除最旧的，**目录最多保留 3 份**
- 控制台仍实时输出（tee：同时写控制台 + 文件，线程安全）
- `data/logs/` 已 gitignore 不入库

#### onboarding 问卷随机不定项选择
注册最后一步 onboarding 第2页问卷有**两组问题**，每组**随机不定项勾选**（至少选 1 项），降低固定选项指纹风险：

- 第1组（8 选项）：Prototype with open models / Flexible capacity for experimentation / Flexible capacity for production / Faster speeds or lower costs / Fine-tune models for quality / High reliability inference for production / Migrate from closed to open models / Migrate from self-hosting to third-party
- 第2组（5 选项）：Code Assistance / Conversational AI / Agentic AI / Search / Multimedia RAG
- 选项文字在 `config.yaml` → `fireworks.form_selectors.onboarding_questionnaire_group1_options` / `group2_options` 配置（按 label 文字定位 checkbox，比动态 id 稳定）

#### 代理与节点切换（Karing）
注册与取 Key 全程经代理 `http://127.0.0.1:3067`（karing 出口端口）发出请求，降低封 IP 风险。每注册一个 key 前由 [`karing_proxy.py`](registrar/karing_proxy.py) 通过 karing 控制端口切换到一个延迟 <400ms 的可用节点，实现「每号换出口 IP」：

- **控制端口 secret 认证**：karing 默认开启 external-controller secret 认证，所有控制 API 请求须带 `Authorization: Bearer <secret>`，否则返回 401。secret 见 karing 安装目录的 `service.json` 的 `"secret"` 字段，填入 `.env` 的 `KARING_SECRET`（或 `config.yaml` → `karing.secret`）。未配置 secret 时控制 API 全部 401，节点切换静默失败、沿用初始出口。
- **Selector 类型组要求**：sing-box 核心仅允许对 `Selector` 类型代理组手动 `PUT` 切换节点（对 `URLTest`/`Fallback` 自动选路组 PUT 返回 400 `Must be a Selector`）。若 karing 配置中无 Selector 组，代码退化为「沿用自动选路 + 测延迟验证出口可用」，**无法实现每号换节点**。如需每号换 IP，请在 karing 配置一个 `Selector` 类型代理组。
- 配置项见 `.env` 的 `KARING_*`（`KARING_ENABLED`/`KARING_API_PORT=3057`/`KARING_PROXY_PORT=3067`/`KARING_SECRET`/`KARING_MAX_LATENCY_MS=400` 等），开关 `KARING_ENABLED=false` 则不切换沿用固定出口。
- 切换失败/无可用节点不阻断造号（warning 沿用当前出口）。

#### 浏览器窗口（headless）
默认 `HEADLESS=true`（不弹出浏览器窗口，无人值守）。headless 模式间歇返回降级 HTML 页（无 React），但代码已有降级页检测 + `max_page_retry=3` 自动重试兜底。如需观察注册流程可设 `HEADLESS=false`（弹窗）。

### 6.1 登录测试（从登录开始，跳过注册）
账号已注册但登录/onboarding 需单独复现时用，密码从 DB 读（明文）：
```bash
cd registrar
python login_test.py --list                    # 列出 DB 中带密码+授权码的账号
python login_test.py --email user@example.com  # 指定邮箱登录测试
python login_test.py --email xxx --password 'xxx' -v  # 手动指定密码+详细日志
```

### 7. 启动 New API 号池（无需 Docker）
```bash
cd pool-gateway
# 下载 new-api Windows 二进制放此目录（见 PROVISIONING.md 4.1）
$env:REGISTER_ENABLED='false'; .\new-api.exe    # SQLite 模式，127.0.0.1:3000
# 首次：POST /api/setup 初始化 root 账号（见 PROVISIONING.md 4.2）
cp newapi.env.example newapi.env                # 填 NEWAPI_ADMIN_USER/PASS
```

### 8. 同步 Key 到号池
```bash
cd pool-gateway
python sync_channels.py        # login 拿 session + 录入渠道（New API v1.0.0-rc.14）
```

### 9. 一键编排（造号+同步+巡检）
```bash
cd orchestrator
python run_pipeline.py
```

## 使用号池（两种入口）

### 进入 New API 后台管理

New API 启动后（`python start.py` 或 `pool-gateway/new-api.exe`），用浏览器访问后台：
- 地址：http://127.0.0.1:3000
- 账号：`root` / 密码：`changeme123`（首次由 `start.py` 的 `POST /api/setup` 自动初始化；也可手动初始化见 [`PROVISIONING.md`](PROVISIONING.md) 4.2）
- 后台可管理：**渠道**（查看/测试/禁用/删除 Fireworks 渠道）、**令牌**（创建/查看 `/v1` 调用用 API token）、**日志**（请求记录/消费/错误）、**设置**（系统配置/模型重定向/分组）
- 渠道对应 `data/keys.json` 里每个 Fireworks key（`sync_channels.py` 自动录入，命名 `fw-<email>`）

### 入口 A：New API 统一入口（加权随机轮询，无限调用）

客户端对接 New API 统一入口（OpenAI 兼容），用 New API「令牌」页创建的 API token。
`start.py` 创建的 token 已设 `unlimited_quota=True` + 永不过期，且自动关闭计费统计
（`configure_unlimited_billing` 关闭消耗日志/额度统计），即**无限调用**（不校验剩余额度、不记录已用）。
token key 默认固定为 `123456`（`--api-key` 可改），故调用 token 为 `sk-123456`：
```bash
curl http://127.0.0.1:3000/v1/chat/completions \
  -H "Authorization: Bearer sk-123456" \
  -H "Content-Type: application/json" \
  -H "X-Custom-Header: anything" \
  -d '{"model":"accounts/fireworks/models/glm-5p2","messages":[{"role":"user","content":"hi"}]}'
```
New API 自动在多个 Fireworks 渠道间加权随机轮询 + 失败重试。

**自定义标头转发**（确保所有 Fireworks 接口能正常转发）：
- 客户端请求自带的任意标头（如 `X-Custom-Header`、`X-Fireworks-*`）：New API 默认透传到上游（`Authorization` 被渠道 key 替换，hop-by-hop 头除外）
- 渠道级固定标头：在 [`pool-gateway/newapi.env`](pool-gateway/newapi.env) 设 `FIREWORKS_CHANNEL_HEADERS`（JSON 字符串），`sync_channels.py` 录入渠道时写入 `channel.header` 字段，New API 转发时附加到上游 Fireworks。例：`FIREWORKS_CHANNEL_HEADERS={"X-Fireworks-Gen-Random-Seed":"42"}`

### 入口 B：sticky 转发代理（同 key 优先 + 连续失败 N 次永久禁用该 key，无限调用纯透传）

Fireworks 有 token 缓存（prefix/KV cache），同一对话用同一 key 命中缓存更快、更省。
New API 原生是加权随机轮询（每次换 key），无法保证粘性。`sticky_proxy.py` 提供第二个入口，
实现 sticky 策略，且**纯透传无任何计费/价格/已用/剩余概念**（天然无限调用）：

`python start.py` 已自动拉起 sticky_proxy(3001)（幂等，已运行则跳过）。也可单独启动：
```bash
cd pool-gateway
python sticky_proxy.py                          # 默认 127.0.0.1:3001, N=3
python sticky_proxy.py --fail-threshold 5 -v    # 失败 5 次永久禁用该 key + 详细日志
```

调用（无需 New API token，代理自动用 Fireworks key，透传所有标头/body）：
```bash
curl http://127.0.0.1:3001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Custom-Header: anything" \
  -d '{"model":"accounts/fireworks/models/glm-5p2","messages":[{"role":"user","content":"hi"}]}'
```

状态查询（当前 sticky key / 失败计数 / 切换历史）：
```bash
curl http://127.0.0.1:3001/sticky/status
```

sticky 策略（**连续失败 → 永久禁用**语义）：
- 正常请求始终用**同一个 key**（命中 Fireworks token 缓存）
- 任何一次成功 → 连续失败计数归零
- 失败（429 / 5xx / 连接 / 超时）→ 连续失败计数 +1（429 不特殊处理，统一计数）
- **连续失败 ≥ N 次**（`--fail-threshold` / `STICKY_FAIL_THRESHOLD`，默认 3）→ **永久禁用当前 key**，切换到下一个**未禁用**的 key
- 被禁用的 key 之后不再被选用（坏 key 自动剔除出轮换池）
- **无可用 key（全部 suspend）→ 报错退出程序**：启动时即全部禁用则 `exit 1`；运行中全部 key 被禁用则关闭服务并 `exit 1`，不再用坏 key 兜底重置
- 切换后新 key 成为 sticky key，继续优先复用
- 所有 Fireworks 接口（`/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`、`/v1/models` 等）均透传，流式 SSE 逐 chunk 转发
- 状态查询 `GET /sticky/status` 返回 `disabled_count` / `disabled_indexes` / `available_keys` / `all_disabled`（已禁用数/下标/可用数/全禁用标志）

**手动标记某 key 永久禁用**（已知坏 key 主动剔除，无需等连续失败 N 次）：
```bash
cd fw-keypool
python -c "import sys; sys.path.insert(0,'registrar'); from state_db import StateDB; \
from config import load_config; cfg=load_config(); \
db=StateDB(cfg.abs_path(cfg.paths.state_db)); \
db.set_key_disabled('xxx@163.com', True, keys_json_path=cfg.abs_path(cfg.paths.keys_json))"
# set_key_disabled(email, True/False, keys_json_path=...) 标记/取消；
# 传入 keys_json_path 时改完 DB 自动重新生成 keys.json（含 disabled 字段），确保 DB↔keys.json 实时一致
```
- 标记后 `data/keys.json` 对应记录含 `"disabled": true`；sticky_proxy 启动时读取该字段**预填充禁用集合**，不再选用该 key
- **数据一致性**：DB 的 `key_disabled` 是 source of truth，keys.json 的 `disabled` 由 `export_keys` 从 DB 生成。`set_key_disabled` 传入 `keys_json_path` 后自动调 `export_keys` 同步，无需手动再调一次
- 运行时连续失败达阈值也会自动永久禁用（见上策略）；两者共用同一 `disabled` 机制，DB 为 source of truth

> 两个入口共用 `data/keys.json`（同一批 Fireworks key），均为**无限调用**（无模型价格/已用/剩余限制），按需选择：
> - New API (3000)：随机轮询 + 管理 UI + 健康巡检 + 计费统计已关闭（unlimited_quota + configure_unlimited_billing）
> - sticky_proxy (3001)：同 key 优先 + token 缓存命中优化 + 纯透传无计费
> - 两入口调用行为均与 Fireworks 官方一致（base_url/模型名/认证格式），仅受上游 Fireworks 账号 credit 限制，多 key 轮换即可视作无限

> 模型名需用 `accounts/fireworks/models/` 前缀（如 `accounts/fireworks/models/glm-5p2`），
> 已在 [`pool-gateway/models.json`](pool-gateway/models.json) 配置。

## 已抓包固化（注册流程）

Fireworks 注册流程已抓包固化到 [`registrar/config.yaml`](registrar/config.yaml)：
- 注册(2步) → 验证邮件(链接) → 登录 → onboarding(2页) → API Key
- 表单选择器全部配置化（email/password/firstName/lastName/Terms/问卷选项）
- 反爬：`add_init_script` 删 `navigator.webdriver`（stealth 禁用，设 false 反触发降级）
- React 受控 input 填值：`_fill_react_input`（nativeInputValueSetter + 重置 `_valueTracker`）
- 降级页检测+重试 / onboarding Continue disabled 检测+刷新重试 / Submit 重复点击等待跳转

## 数据持久化

SQLite 状态库（`data/state.db`）记录每个邮箱 job：
- 邮箱连接信息（protocol/host/port/username/**auth_code 授权码**）
- 注册密码（明文持久化，重试复用，避免密码不一致导致登录 Invalid）
- Fireworks user_id / API Key / 代理 / 重试次数 / 错误信息

`python login_test.py --list` 明文显示 email/password/auth_code，便于核对。
`keys.json` 导出含 auth_code，便于后续复用邮箱收信。

详见 [`PROVISIONING.md`](PROVISIONING.md)。

## License

本项目采用 [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0)](https://creativecommons.org/licenses/by-nc-sa/4.0/) 协议授权。

- **署名（BY）**：使用时须保留原作者署名 + 协议链接 + 标注修改
- **非商业（NC）**：禁止商业使用
- **相同方式共享（SA）**：衍生作品须以相同协议授权

完整协议文本见 [`LICENSE`](LICENSE)。
本项目另受 [`RISK_NOTICE.md`](RISK_NOTICE.md) 免责声明约束，仅供学习研究用途。
协议与免责声明如有冲突，以 `RISK_NOTICE.md` 为准。

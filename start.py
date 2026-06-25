"""fw-keypool 一键启动脚本：启动 New API + 初始化 + 录入邮箱池 + 注册造号 + 同步渠道。

功能：
1. 启动 New API（pool-gateway/new-api 或 new-api.exe，SQLite 模式，若未运行则后台启动）
2. 初始化 New API（POST /api/setup 创建 root + 生成 API token，若首次）
3. 从 emails.csv 加载邮箱池入库（registrar run.py --load-only）
4. 注册造号（registrar run.py --limit N）
5. sync_channels 录入渠道到 New API
6. 输出 API token + 调用示例

用法：
    python start.py                         # 全流程（启动+初始化+造号+同步）
    python start.py --limit 1               # 只造 1 个号
    python start.py --newapi-key sk-xxx     # 指定已有 API token（跳过生成）
    python start.py --skip-register         # 跳过造号（仅启动+同步已有 key）
    python start.py --skip-newapi           # 跳过 New API 启动（已手动启动）
    python start.py -v                      # 详细日志
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from log_system import setup_console_logging

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
REGISTRAR = ROOT / "registrar"
POOL = ROOT / "pool-gateway"
NEWAPI_ENV = POOL / "newapi.env"
STATE_DB = ROOT / "data" / "state.db"
KEYS_JSON = ROOT / "data" / "keys.json"
STICKY_PROXY = POOL / "sticky_proxy.py"

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    load_dotenv(NEWAPI_ENV)
except Exception:
    pass

# New API 默认配置（首次初始化用）
NEWAPI_BASE_URL = os.getenv("NEWAPI_BASE_URL", "http://127.0.0.1:3000")
NEWAPI_ROOT_USER = os.getenv("NEWAPI_ADMIN_USER", "root")
NEWAPI_ROOT_PASS = os.getenv("NEWAPI_ADMIN_PASS", "changeme123")
# 固定 API token 的 key（生成的 token key 固定为此值，方便记忆/测试；可用 --api-key 修改）
# 最终调用 token 形如 sk-123456。New API AddToken 后端自动生成 key，故创建后直接改 DB。
DEFAULT_FIXED_API_KEY = "123456"
# sticky_proxy 默认配置（无限调用纯透传入口）
STICKY_PROXY_HOST = os.getenv("STICKY_PROXY_HOST", "127.0.0.1")
STICKY_PROXY_PORT = int(os.getenv("STICKY_PROXY_PORT", "3001"))
STICKY_PROXY_URL = f"http://{STICKY_PROXY_HOST}:{STICKY_PROXY_PORT}"
STICKY_FAIL_THRESHOLD = int(os.getenv("STICKY_FAIL_THRESHOLD", "3"))
STICKY_UPSTREAM_TIMEOUT = int(os.getenv("STICKY_UPSTREAM_TIMEOUT", "120"))


def _newapi_binary() -> Path:
    """Return the New API binary path for the current platform.

    `NEWAPI_BIN` can point to a custom binary. Otherwise Linux/macOS use
    pool-gateway/new-api and Windows keeps the historical new-api.exe name.
    """
    override = os.getenv("NEWAPI_BIN", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        return POOL / "new-api.exe"
    return POOL / "new-api"


def _open_process_log(name: str):
    log_dir = ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return (log_dir / name).open("ab", buffering=0)


def _popen_detached(cmd: list[str], cwd: Path, env: dict[str, str] | None, log_name: str) -> subprocess.Popen:
    """Start a long-running child process in the background on Windows/Linux."""
    log_file = _open_process_log(log_name)
    kwargs: dict = {
        "cwd": str(cwd),
        "env": env,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _run(cmd: list[str], cwd: Path, label: str, check: bool = True) -> bool:
    logger.info("▶ %s: %s", label, " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(cwd), check=False)
    if r.returncode != 0:
        logger.error("✗ %s 失败（exit %d）", label, r.returncode)
        if check:
            return False
    else:
        logger.info("✓ %s 完成", label)
    return r.returncode == 0


def newapi_is_running() -> bool:
    """检查 New API 是否在运行。"""
    try:
        httpx.get(f"{NEWAPI_BASE_URL}/api/status", timeout=5)
        return True
    except Exception:
        return False


def start_newapi() -> bool:
    """后台启动 New API（跨平台二进制，SQLite 模式）。"""
    if newapi_is_running():
        logger.info("✓ New API 已在运行（%s）", NEWAPI_BASE_URL)
        return True
    newapi_bin = _newapi_binary()
    if not newapi_bin.exists():
        logger.error("❌ New API 二进制不存在: %s", newapi_bin)
        if os.name == "nt":
            logger.error("   下载: https://github.com/QuantumNous/new-api/releases/download/v1.0.0-rc.14/new-api-v1.0.0-rc.14.exe")
        else:
            logger.error("   Linux 可执行: bash scripts/install_newapi_linux.sh")
        return False
    if os.name != "nt" and not os.access(newapi_bin, os.X_OK):
        logger.error("❌ New API 二进制不可执行: %s（请 chmod +x 或运行 scripts/install_newapi_linux.sh）", newapi_bin)
        return False
    logger.info("▶ 启动 New API（SQLite 模式，%s）", newapi_bin)
    env = os.environ.copy()
    env.pop("SQL_DSN", None)  # 不设才走 SQLite
    env["REGISTER_ENABLED"] = "false"
    env["TZ"] = "Asia/Shanghai"
    _popen_detached([str(newapi_bin)], cwd=POOL, env=env, log_name="new-api.log")
    # 等待启动
    for i in range(15):
        time.sleep(2)
        if newapi_is_running():
            logger.info("✓ New API 已启动（等待 %ds）", (i + 1) * 2)
            return True
    logger.error("❌ New API 启动超时（30s 未响应）")
    return False


def sticky_proxy_is_running() -> bool:
    """检查 sticky_proxy 是否在运行（GET /sticky/status）。"""
    try:
        httpx.get(f"{STICKY_PROXY_URL}/sticky/status", timeout=5)
        return True
    except Exception:
        return False


def start_sticky_proxy() -> bool:
    """后台启动 sticky_proxy（无限调用纯透传入口，127.0.0.1:3001）。

    sticky_proxy 与 New API 并存：New API (3000) 有计费/管理 UI，sticky_proxy (3001)
    纯透传无计费。两者共用 keys.json。本函数确保一键启动后 3001 立即可用，避免"连不上"。
    """
    if sticky_proxy_is_running():
        logger.info("✓ sticky_proxy 已在运行（%s）", STICKY_PROXY_URL)
        return True
    if not KEYS_JSON.exists():
        logger.warning("• keys.json 不存在，跳过 sticky_proxy 启动（无 key 可用）")
        return False
    if not STICKY_PROXY.exists():
        logger.error("❌ sticky_proxy.py 不存在: %s", STICKY_PROXY)
        return False
    logger.info("▶ 启动 sticky_proxy（纯透传无限入口，%s）", STICKY_PROXY_URL)
    _popen_detached(
        [
            sys.executable,
            str(STICKY_PROXY),
            "--host", STICKY_PROXY_HOST,
            "--port", str(STICKY_PROXY_PORT),
            "--keys", str(KEYS_JSON),
            "--state-db", str(STATE_DB),
            "--fail-threshold", str(STICKY_FAIL_THRESHOLD),
            "--timeout", str(STICKY_UPSTREAM_TIMEOUT),
        ],
        cwd=ROOT,
        env=os.environ.copy(),
        log_name="sticky_proxy.log",
    )
    # 等待启动
    for i in range(10):
        time.sleep(1)
        if sticky_proxy_is_running():
            logger.info("✓ sticky_proxy 已启动（等待 %ds，%s/v1/*）", i + 1, STICKY_PROXY_URL)
            return True
    logger.error("❌ sticky_proxy 启动超时（10s 未响应）")
    return False


def init_newapi(fixed_api_key: str = DEFAULT_FIXED_API_KEY) -> str | None:
    """初始化 New API：setup root + 生成 API token + 固定 token key + 关闭计费限制（无限调用）。

    返回 API token（sk- 前缀，默认 sk-123456）。

    固定 API key：
    - New API AddToken 后端自动生成随机 key，无法在创建时指定。
    - 本函数创建 token 后直接 UPDATE one-api.db tokens 表把 key 改为固定值（默认 123456），
      方便记忆/测试。可用 --api-key 参数或环境变量 FW_FIXED_API_KEY 修改。
    - 最终调用 token 形如 sk-123456。

    无限调用实现（删除模型价格/已用/剩余对调用的限制）：
    - Token 层：unlimited_quota=True + expired_time=-1 → New API 转发时不校验剩余额度，永不拒绝。
    - 计费层：调用 configure_unlimited_billing() 关闭消耗日志记录 / 额度统计，使"已用/剩余"
      不再累积、不再展示为限制（New API 为闭源 Go 二进制，UI 计费展示通过系统 option 开关控制）。
    - 渠道层：sync_channels.py 创建渠道时不设任何价格/配额字段（OpenAI 兼容渠道无此类字段）。
    最终 New API 入口与 sticky_proxy 入口同为"无限调用"：仅受上游 Fireworks 账号 credit 限制，
      号池多 key 轮换即可视作无限。
    """
    fixed_api_key = (fixed_api_key or "").strip() or DEFAULT_FIXED_API_KEY
    c = httpx.Client(base_url=NEWAPI_BASE_URL, timeout=15)
    # 1. 尝试 setup（首次有效，已初始化则忽略错误）
    try:
        r = c.post("/api/setup", json={
            "username": NEWAPI_ROOT_USER, "password": NEWAPI_ROOT_PASS,
            "confirmPassword": NEWAPI_ROOT_PASS, "SelfUseModeEnabled": True, "DemoSiteEnabled": False,
        })
        j = r.json()
        if j.get("success"):
            logger.info("✓ New API root 账号初始化成功")
        else:
            logger.info("• New API 已初始化（setup 返回: %s）", j.get("message", ""))
    except Exception as e:
        logger.warning("setup 异常（可能已初始化）: %s", e)

    # 2. login 拿 session
    try:
        c.post("/api/user/login", json={"username": NEWAPI_ROOT_USER, "password": NEWAPI_ROOT_PASS})
    except Exception as e:
        logger.error("❌ New API login 失败: %s", e)
        return None

    h = {"New-Api-User": "1"}

    # 3. 关闭计费/价格/额度统计（无限调用：不记录已用、不展示剩余限制）
    configure_unlimited_billing(c, h)
    configure_private_instance(c, h)

    # 4. 查是否已有 API token
    try:
        r = c.get("/api/token/?p=1&page_size=10", headers=h)
        items = r.json().get("data", {}).get("items", []) or []
        if items:
            tid = items[0]["id"]
            # 已有 token，确保其为无限配额（兜底：把旧 token 也升级为 unlimited）
            _ensure_token_unlimited(c, h, tid)
            # 固定 token key 为指定值（默认 123456）
            if _fix_token_key(tid, fixed_api_key):
                logger.info("✓ API token key 已固定为 %s（sk-%s）", fixed_api_key, fixed_api_key)
            return f"sk-{fixed_api_key}"
    except Exception:
        pass

    # 5. 创建 API token（unlimited_quota：无限额度，永不过期 → 无限调用）
    try:
        c.post("/api/token/", json={
            "name": "fw-pool-auto", "expired_time": -1,   # -1 = 永不过期
            "unlimited_quota": True,                       # 无限额度：不校验 remain_quota，永不拒绝
            "remain_quota": 0, "group": "default",
        }, headers=h)
        # 列出取 id
        r = c.get("/api/token/?p=1&page_size=10", headers=h)
        items = r.json().get("data", {}).get("items", []) or []
        if items:
            tid = items[-1]["id"]
            if _fix_token_key(tid, fixed_api_key):
                logger.info("✓ API token key 已固定为 %s（sk-%s）", fixed_api_key, fixed_api_key)
            return f"sk-{fixed_api_key}"
    except Exception as e:
        logger.error("❌ 创建 API token 失败: %s", e)
    return None


# 计费/价格/额度相关 option key 关键词（用于动态发现 New API 系统设置项）。
# New API 为闭源 Go 二进制，option key 名随版本变化，故用关键词匹配动态发现，
# 避免硬编码错误 key 名导致 API 报错。
_BILLING_OPTION_KEYWORDS = (
    "logconsume", "log_consume", "consume",        # 消耗日志记录（已用统计）
    "quota", "billing", "price", "ratio",          # 额度/计费/价格/比率
    "display", "show",                             # UI 展示开关
)


def configure_unlimited_billing(client: httpx.Client, headers: dict) -> None:
    """关闭 New API 计费/价格/已用/剩余统计，实现无限调用（不阻断、不记录消耗）。

    New API 闭源，option key 名随版本变化。本函数先 GET /api/option 读取全部系统设置，
    动态匹配计费/价格/额度相关 key，再 PUT 关闭或置零。所有操作 try/except 包裹，
    失败仅告警不阻断主流程（token unlimited_quota 已保证调用不被拒）。

    目标：删除"模型价格/已用/剩余"对调用的限制与统计展示。
    """
    try:
        r = client.get("/api/option/", headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json().get("data", []) or []
    except Exception as e:
        logger.warning("• 读取 New API 系统设置失败（跳过计费关闭，token 无限仍生效）: %s", e)
        return

    # data 通常是 [{key, value}, ...] 或 {key: value, ...}
    opts: dict[str, str] = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "key" in item:
                opts[str(item["key"])] = str(item.get("value", ""))
    elif isinstance(data, dict):
        opts = {str(k): str(v) for k, v in data.items()}

    if not opts:
        logger.info("• New API 无可配置系统设置（token 无限配额已保证无限调用）")
        return

    # 动态发现计费/价格/额度相关 option
    targets: dict[str, str] = {}
    for k, v in opts.items():
        kl = k.lower()
        if any(kw in kl for kw in _BILLING_OPTION_KEYWORDS):
            # 布尔类开关 → 关闭（false）；数值类（ratio/price）→ 置 0（免费/不计费）
            if kl.endswith(("enabled", "show", "display", "logconsume", "log_consume")) or v.lower() in ("true", "false"):
                targets[k] = "false"
            else:
                targets[k] = "0"

    if not targets:
        logger.info("• New API 未发现计费/价格/额度设置项（token 无限配额已保证无限调用）")
        return

    success = 0
    for k, v in targets.items():
        try:
            client.put("/api/option/", json={"key": k, "value": v}, headers=headers, timeout=15)
            success += 1
        except Exception as e:
            logger.debug("设置 %s=%s 失败: %s", k, v, e)
    logger.info("✓ 已关闭 New API 计费/价格/额度统计 %d 项（无限调用：不记录已用/不展示剩余限制）", success)


def configure_private_instance(client: httpx.Client, headers: dict) -> None:
    """Disable public self-registration options on the local New API instance."""
    for key in ("RegisterEnabled", "PasswordRegisterEnabled"):
        try:
            client.put("/api/option/", json={"key": key, "value": "false"}, headers=headers, timeout=15)
        except Exception as e:
            logger.debug("关闭 New API 注册选项 %s 失败: %s", key, e)


def _ensure_token_unlimited(client: httpx.Client, headers: dict, token_id: int) -> None:
    """确保已有 token 为无限配额（兜底：把旧 token 升级为 unlimited_quota + 永不过期）。"""
    try:
        client.put("/api/token/", json={
            "id": token_id, "expired_time": -1,
            "unlimited_quota": True, "remain_quota": 0,
        }, headers=headers, timeout=15)
    except Exception as e:
        logger.debug("升级 token %d 为无限配额失败（可能已是无限）: %s", token_id, e)


def _read_token_from_db(token_id: int) -> str | None:
    """从 one-api.db tokens 表读完整 key。"""
    db = POOL / "one-api.db"
    if not db.exists():
        return None
    try:
        import sqlite3
        c = sqlite3.connect(str(db))
        row = c.execute("SELECT key FROM tokens WHERE id = ?", (token_id,)).fetchone()
        c.close()
        if row:
            return f"sk-{row[0]}"
    except Exception as e:
        logger.warning("读 token DB 失败: %s", e)
    return None


def _fix_token_key(token_id: int, fixed_key: str) -> bool:
    """把指定 token 的 key 固定为 fixed_key（直接改 one-api.db tokens 表）。

    New API AddToken 后端自动生成随机 key，无法在创建时指定，故创建后直接 UPDATE DB。
    tokens.key 有唯一索引，若 fixed_key 已被其他 token 占用，先删除占用 token 再改。

    注意：New API 可能有内存缓存，改 DB 后可能需重启 New API 才生效（调用方应在改后提示）。
    返回是否成功。
    """
    db = POOL / "one-api.db"
    if not db.exists():
        logger.warning("• one-api.db 不存在，无法固定 token key（返回固定值供调用，但 DB 未改）")
        return False
    try:
        import sqlite3
        c = sqlite3.connect(str(db))
        # 1. 若该 token key 已是 fixed_key，无需改
        row = c.execute("SELECT key FROM tokens WHERE id = ?", (token_id,)).fetchone()
        if row and row[0] == fixed_key:
            c.close()
            return True
        # 2. 清理占用 fixed_key 的其他 token（唯一索引冲突避免）
        c.execute("DELETE FROM tokens WHERE key = ? AND id != ?", (fixed_key, token_id))
        # 3. 改目标 token key
        c.execute("UPDATE tokens SET key = ? WHERE id = ?", (fixed_key, token_id))
        c.commit()
        c.close()
        logger.info("✓ token(id=%d) key 已固定为 %s", token_id, fixed_key)
        return True
    except Exception as e:
        logger.warning("• 固定 token key 失败: %s（可能需重启 New API 让缓存刷新）", e)
        return False


def ensure_newapi_env(api_token: str | None) -> None:
    """确保 newapi.env 存在且含 ADMIN_USER/PASS（sync_channels 用）。"""
    defaults = {
        "NEWAPI_BASE_URL": NEWAPI_BASE_URL,
        "NEWAPI_ADMIN_USER": NEWAPI_ROOT_USER,
        "NEWAPI_ADMIN_PASS": NEWAPI_ROOT_PASS,
        "NEWAPI_ACCESS_TOKEN": api_token or "",
        "FIREWORKS_BASE_URL": "https://api.fireworks.ai/inference",
        "CHANNEL_GROUP": "default",
        "CHANNEL_PRIORITY": "0",
        "CHANNEL_WEIGHT": "100",
    }
    if not NEWAPI_ENV.exists():
        NEWAPI_ENV.write_text("".join(f"{k}={v}\n" for k, v in defaults.items()), encoding="utf-8")
        logger.info("✓ 已生成 %s", NEWAPI_ENV)
        return

    lines = NEWAPI_ENV.read_text(encoding="utf-8").splitlines()
    present = {
        line.split("=", 1)[0].strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    missing = [k for k in defaults if k not in present]
    if not missing:
        return
    with NEWAPI_ENV.open("a", encoding="utf-8") as f:
        if lines and lines[-1].strip():
            f.write("\n")
        f.write("\n# Added by start.py for Linux/headless deployment\n")
        for key in missing:
            f.write(f"{key}={defaults[key]}\n")
    logger.info("✓ 已补齐 %s 缺失配置: %s", NEWAPI_ENV, ", ".join(missing))


def main() -> int:
    ap = argparse.ArgumentParser(description="fw-keypool 一键启动（New API + sticky_proxy + 造号 + 同步）")
    ap.add_argument("--limit", type=int, default=None, help="造号上限")
    ap.add_argument("--newapi-key", default=None, help="指定已有 New API API token（sk-xxx，跳过生成+固定）")
    ap.add_argument("--api-key", default=os.getenv("FW_FIXED_API_KEY", DEFAULT_FIXED_API_KEY),
                    help=f"固定生成的 API token key（默认 {DEFAULT_FIXED_API_KEY}，即 sk-{DEFAULT_FIXED_API_KEY}；"
                         f"可用环境变量 FW_FIXED_API_KEY 覆盖）")
    ap.add_argument("--skip-newapi", action="store_true", help="跳过 New API 启动（已手动启动）")
    ap.add_argument("--skip-sticky", action="store_true", help="跳过 sticky_proxy 启动（3001 纯透传无限入口）")
    ap.add_argument("--skip-register", action="store_true", help="跳过造号（仅启动+同步已有 key）")
    ap.add_argument("--skip-sync", action="store_true", help="跳过 sync_channels")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    # 日志系统：控制台输出 + 保存到文件（最多留 3 份），Windows GBK 终端兼容内置
    setup_console_logging(
        log_dir=ROOT / "data" / "logs",
        max_files=3,
        verbose=args.verbose,
    )
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ok = True

    # 1. 启动 New API
    api_token = args.newapi_key
    if not args.skip_newapi:
        if not start_newapi():
            return 1
        if not api_token:
            api_token = init_newapi(fixed_api_key=args.api_key)
            if not api_token:
                logger.error("❌ 未获取到 API token，可用 --newapi-key 指定")
                return 1
            logger.info("✓ New API API token: %s", api_token)
        ensure_newapi_env(api_token)

    # 2. 启动 sticky_proxy（无限调用纯透传入口，3001）
    if not args.skip_sticky:
        if not start_sticky_proxy():
            logger.warning("• sticky_proxy 启动失败（3001 入口暂不可用，New API 3000 仍可用）")
            ok = False

    # 3. 造号（从 emails.csv 加载 + 注册）
    if not args.skip_register:
        load_cmd = [sys.executable, "run.py", "--load-only"]
        if not _run(load_cmd, REGISTRAR, "加载邮箱池入库"):
            ok = False
        else:
            reg_cmd = [sys.executable, "run.py"]
            if args.limit:
                reg_cmd += ["--limit", str(args.limit)]
            _run(reg_cmd, REGISTRAR, "注册造号")

    # 4. 同步渠道
    if not args.skip_sync:
        _run([sys.executable, "sync_channels.py"], POOL, "同步渠道到 New API")

    # 4. 输出结果
    print("\n" + "=" * 60)
    print("fw-keypool 一键启动完成（无限调用：无模型价格/已用/剩余限制）")
    print("=" * 60)
    if api_token:
        print(f"\n[入口1] New API（无限配额 token，已关闭计费统计）")
        print(f"New API API token: {api_token}")
        print(f"调用入口: POST {NEWAPI_BASE_URL}/v1/chat/completions")
        print("调用示例:")
        print(f'  curl {NEWAPI_BASE_URL}/v1/chat/completions \\')
        print(f'    -H "Authorization: Bearer {api_token}" \\')
        print('    -H "Content-Type: application/json" \\')
        print('    -d \'{"model":"accounts/fireworks/models/glm-5p2","messages":[{"role":"user","content":"hi"}]}\'')
    print(f"\n[入口2] sticky_proxy（纯透传，无计费/价格/已用/剩余，无限调用）")
    print(f"调用入口: POST http://127.0.0.1:3001/v1/chat/completions")
    print("调用示例:")
    print('  curl http://127.0.0.1:3001/v1/chat/completions \\')
    print('    -H "Content-Type: application/json" \\')
    print('    -d \'{"model":"accounts/fireworks/models/glm-5p2","messages":[{"role":"user","content":"hi"}]}\'')
    print("  启动: python pool-gateway/sticky_proxy.py")
    if KEYS_JSON.exists():
        import json
        keys = json.loads(KEYS_JSON.read_text(encoding="utf-8"))
        # 号池渠道数只统计可用（非 disabled）key，与 sync_channels 录入过滤逻辑一致：
        # disabled 的 key 不会被录入渠道，故不计入号池渠道数。
        usable = [r for r in keys if not r.get("disabled")]
        disabled_count = len(keys) - len(usable)
        print(f"\n号池渠道数: {len(usable)}（可用 {len(usable)}，禁用 {disabled_count}/{len(keys)}）")
        for k in usable:
            print(f"  {k['email']}  key={k['api_key'][:12]}...")
        if disabled_count:
            print(f"  （另有 {disabled_count} 个已禁用 key 未计入号池渠道数）")
    print("\n说明: 两入口均与 Fireworks 官方调用行为一致（base_url/模型名/认证格式）")
    print("  token 已设 unlimited_quota=True + 永不过期 + 计费统计已关闭 → 无限调用")
    print("  仅受上游 Fireworks 账号 credit 限制，号池多 key 轮换即可视作无限")
    if not ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

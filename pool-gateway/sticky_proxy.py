"""sticky_proxy.py — Fireworks sticky 转发代理（同 key 优先 + 连续失败 N 次永久禁用该 key）。

## 为什么需要
Fireworks 有 token 缓存（prefix cache / KV cache）：同一对话/相同前缀用同一 API key
命中缓存更快、更省。New API 原生是"加权随机轮询"，每次请求随机选渠道（换 key），
无法保证同一会话粘在同一 key，缓存命中率低。

本代理在 New API 之外提供**第二个统一入口**，实现 sticky 策略：
- 正常请求始终用**同一个 key**（命中 Fireworks token 缓存）
- 该 key 连续失败 ≥ N 次 → **永久禁用该 key**，切换到下一个可用 key（429 限速同样计入失败次数，不立即换）
- 切换后新 key 成为 sticky key，继续优先复用；被禁用的 key 之后不再被选用
- 无可用 key（全部 suspend）：启动时即全部禁用 → 报错退出；运行中全部被禁用 →
  关闭服务并退出（exit 1），不再用坏 key 兜底重置

## 工作方式
- 监听 127.0.0.1:PORT（默认 3001，避开 New API 3000）
- 路径 /v1/<path> 透传到 https://api.fireworks.ai/inference/v1/<path>
- Authorization 替换为当前 sticky key，**透传所有其他 header / body / query**
- 流式（SSE）响应逐 chunk 转发（httpx.stream + wfile + flush）
- 所有 Fireworks 接口（chat/completions、completions、embeddings、models 等）均透传
- **无可用 key（全部 suspend）**：启动时即全部禁用 → 报错退出(exit 1)；
  运行中全部 key 被禁用 → 关闭服务并退出(exit 1)，不再用坏 key 兜底重置

## 用法
    python sticky_proxy.py                              # 默认 127.0.0.1:3001, N=3
    python sticky_proxy.py --port 3001 --fail-threshold 3
    python sticky_proxy.py --keys ../data/keys.json -v

调用示例（与 New API 入口用法一致，但无需 New API token，代理自动用 Fireworks key）：
    curl http://127.0.0.1:3001/v1/chat/completions \\
      -H "Content-Type: application/json" \\
      -H "X-Custom-Header: anything" \\
      -d '{"model":"accounts/fireworks/models/glm-5p2","messages":[{"role":"user","content":"hi"}]}'

## 与 New API 的关系
- New API (3000)：加权随机轮询 + 管理 UI + 健康巡检 + 计费（原有，保留）
- sticky_proxy (3001)：同 key 优先 + 连续失败 N 次永久禁用该 key（新增，token 缓存优化）
- 两者共用 keys.json（同一批 Fireworks key），按需选择入口
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("sticky_proxy")

# 项目根在 pool-gateway/ 上一级，加入 sys.path 以便 import log_system
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from log_system import setup_console_logging  # noqa: E402

# registrar 同级目录，用于导入 StateDB 回写运行时禁用状态到 DB + keys.json
_REGISTRAR_DIR = _PROJECT_ROOT / "registrar"
if str(_REGISTRAR_DIR) not in sys.path:
    sys.path.insert(0, str(_REGISTRAR_DIR))
try:
    from state_db import StateDB  # noqa: E402
except Exception:  # pragma: no cover - registrar 缺失时降级为纯内存模式
    StateDB = None  # type: ignore[assignment]

# 转发到上游时剥除的 hop-by-hop / 冲突头（Authorization 由 sticky key 注入）
_HOP_BY_HOP = {
    "host", "content-length", "transfer-encoding", "connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade",
    "authorization",  # 由 sticky key 替换
}
# 上游响应回传时剥除的头（由 BaseHTTPRequestHandler 自行处理 / 避免重复）
_RESP_STRIP = {
    "transfer-encoding", "connection", "keep-alive", "content-encoding",
    "content-length",  # 流式时长度未知，由 chunked 或客户端处理
}


class StickyKeySelector:
    """线程安全的 sticky key 选择器：同 key 优先，失败超 N 次**永久禁用**该 key。

    状态机：
    - current_index：当前 sticky key 在 keys 列表中的下标
    - fail_count：当前 key 连续失败计数
    - disabled：被永久禁用的 key 下标集合（连续失败达阈值后加入，之后不再选用）
    - 成功 → fail_count = 0
    - 失败（429 / 5xx / 连接错误 / 超时）→ fail_count += 1（429 不特殊处理，统一计数）
    - fail_count >= threshold → 永久禁用当前 key，切换到下一个**未禁用**的 key，fail_count 归零；
      若持有 StateDB 句柄 + index_to_email 映射，则同步回写 DB 的 key_disabled 并重新
      export keys.json，确保运行时禁用状态持久化（重启不丢失，DB 与 keys.json 实时一致）
    - 所有 key 都被禁用 → 置 all_disabled=True（不再用坏 key 兜底重置；由上层关闭服务并退出程序）
    """

    def __init__(self, keys: list[str], fail_threshold: int = 3, *,
                 index_to_email: dict[int, str] | None = None,
                 db: "StateDB | None" = None,
                 keys_json_path: str | Path | None = None) -> None:
        if not keys:
            raise ValueError("keys 列表为空，sticky_proxy 无法工作")
        self.keys = [k for k in keys if k and k.strip()]
        if not self.keys:
            raise ValueError("keys 列表无有效 key")
        self.fail_threshold = max(1, fail_threshold)
        self.current_index = 0
        self.fail_count = 0
        self.disabled: set[int] = set()
        self.all_disabled: bool = False  # 所有 key 均被禁用标志（运行中触发 → 关闭服务退出）
        self._lock = threading.Lock()
        # 切换历史：记录 (时间戳, 旧下标, 新下标, 原因)
        self.switch_history: list[tuple[float, int, int, str]] = []
        # 运行时禁用回写所需：下标→email 映射 + StateDB 句柄 + keys.json 路径
        # 三者齐备时，on_failure 永久禁用会同步 DB key_disabled + 重新 export keys.json，
        # 避免"运行期禁用只存内存、重启丢失、DB 与 keys.json 不同步"的 bug
        self.index_to_email = index_to_email or {}
        self.db = db
        self.keys_json_path = keys_json_path

    def _persist_disable(self, idx: int) -> None:
        """把运行时永久禁用的 key 回写到 state.db + 重新 export keys.json。

        无锁调用（调用方 on_failure 已持锁）；回写失败仅记日志不抛异常，避免影响转发。
        DB 是 source of truth，set_key_disabled(email, True, keys_json_path) 会改 DB
        的 key_disabled 并自动 export keys.json，保证两者一致。
        """
        email = self.index_to_email.get(idx)
        if not email or self.db is None or not self.keys_json_path:
            # 缺少回写条件（无 email 映射 / 无 DB 句柄 / 无 keys.json 路径）：仅内存禁用
            logger.warning(
                "⚠️ key 下标 %d 已内存禁用但未持久化（缺少 email 映射/DB/keys.json 路径），"
                "重启后该禁用将丢失", idx)
            return
        try:
            updated = self.db.set_key_disabled(email, True, self.keys_json_path)
            if updated:
                logger.info("💾 已持久化禁用 key(email=%s) → DB key_disabled=1 并同步 keys.json",
                            email)
            else:
                logger.warning("⚠️ set_key_disabled 未更新任何行（email=%s 不在 DB？）", email)
        except Exception as e:
            logger.error("❌ 回写 DB 禁用状态失败(email=%s): %s（仅内存禁用，重启将丢失）",
                         email, e)

    def current_key(self) -> str:
        with self._lock:
            return self.keys[self.current_index]

    def _next_available(self, from_index: int) -> int | None:
        """从 from_index 起向后查找第一个未禁用的下标；全禁用返回 None。"""
        n = len(self.keys)
        for off in range(n):
            idx = (from_index + off) % n
            if idx not in self.disabled:
                return idx
        return None

    def on_success(self) -> None:
        with self._lock:
            if self.fail_count != 0:
                logger.debug("sticky key 复位失败计数（成功）: %s...",
                             self.keys[self.current_index][:10])
            self.fail_count = 0

    def on_failure(self, status_code: int | None = None) -> str:
        """记录失败，返回失败后应使用的 key（可能已切换/禁用旧 key）。

        统一按失败计数处理：任何失败（含 429 限速 / 5xx / 连接 / 超时）
        均 fail_count += 1，达 fail_threshold 阈值则**永久禁用当前 key**
        并切换到下一个未禁用的 key。不对 429 特殊处理（保持同 key 优先，命中 token 缓存）。
        所有 key 均被禁用时置 all_disabled=True（不再用坏 key 兜底重置；
        由 _proxy 检测该标志后关闭服务，main 以 exit 1 退出程序）。
        """
        with self._lock:
            old_index = self.current_index
            switched = False
            reason = ""
            self.fail_count += 1
            if self.fail_count >= self.fail_threshold:
                # 永久禁用当前 key
                self.disabled.add(old_index)
                # 持久化回写：DB key_disabled=1 + 重新 export keys.json，
                # 避免"运行期禁用只存内存、重启丢失、DB 与 keys.json 不同步"
                self._persist_disable(old_index)
                # 选下一个未禁用的 key
                nxt = self._next_available((old_index + 1) % len(self.keys))
                if nxt is None:
                    # 所有 key 都被禁用：不再兜底重置，置标志由上层关闭服务并退出程序
                    self.all_disabled = True
                    logger.critical(
                        "🚫 所有 %d 个 key 均已被禁用（全部 suspend），无可用 key，"
                        "关闭服务并退出程序", len(self.keys))
                    # current_index 保持旧值（已禁用），上层不会再用它转发
                    self.fail_count = 0
                    return self.keys[old_index]
                self.current_index = nxt
                self.fail_count = 0
                switched = True
                reason = f"fail-{self.fail_threshold}+disabled"
            if switched:
                self.switch_history.append(
                    (time.time(), old_index, self.current_index, reason))
                logger.warning(
                    "🔄 切换 sticky key: %s... → %s... (原因=%s, 已禁用=%d/%d, 累计切换=%d)",
                    self.keys[old_index][:10], self.keys[self.current_index][:10],
                    reason, len(self.disabled), len(self.keys),
                    len(self.switch_history))
            return self.keys[self.current_index]

    def is_all_disabled(self) -> bool:
        """是否所有 key 均被禁用（运行中触发，应关闭服务退出）。"""
        with self._lock:
            return self.all_disabled

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "current_index": self.current_index,
                "current_key_prefix": self.keys[self.current_index][:10] + "...",
                "fail_count": self.fail_count,
                "fail_threshold": self.fail_threshold,
                "total_keys": len(self.keys),
                "disabled_count": len(self.disabled),
                "disabled_indexes": sorted(self.disabled),
                "available_keys": len(self.keys) - len(self.disabled),
                "all_disabled": self.all_disabled,
                "switch_count": len(self.switch_history),
                "switch_history": self.switch_history[-10:],
            }


class StickyProxyHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器：透传 /v1/* 到 Fireworks，Authorization 用 sticky key。

    ThreadingHTTPServer 每请求一线程，handler 实例独立。sticky 选择器通过
    server.selector 共享（线程安全）。
    """

    # 静默默认日志（用 logger 自定义输出）
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("%s - %s", self.address_string(), format % args)

    # ---- 共用转发逻辑 ----
    def _proxy(self, method: str) -> None:
        selector: StickyKeySelector = self.server.selector  # type: ignore[attr-defined]
        upstream: str = self.server.upstream  # type: ignore[attr-defined]
        timeout: float = self.server.timeout_upstream  # type: ignore[attr-defined]

        # 所有 key 均被禁用（全部 suspend）：报错并关闭服务退出程序
        if selector.is_all_disabled():
            logger.critical("🚫 无可用 key（全部 suspend），拒绝转发，关闭服务")
            self._send_error(503, "all keys disabled (suspended), no usable key")
            self._shutdown_server()
            return

        # 构造上游 URL：/v1/<path> → <upstream>/v1/<path>
        # upstream 形如 https://api.fireworks.ai/inference/v1（已含 /v1）
        # 请求路径形如 /v1/chat/completions，直接拼到 upstream 根
        path = self.path
        # upstream 末尾去掉 /v1（如果有），再拼请求路径（请求路径含 /v1）
        upstream_base = upstream.rstrip("/")
        if upstream_base.endswith("/v1"):
            upstream_base = upstream_base[:-3]
        url = upstream_base + path

        # 读 body
        body = b""
        cl = self.headers.get("Content-Length")
        if cl:
            try:
                body = self.rfile.read(int(cl))
            except Exception as e:
                logger.error("读请求 body 失败: %s", e)
                self._send_error(400, f"read body failed: {e}")
                return

        # 构造上游 headers：透传所有非 hop-by-hop 头
        fwd_headers: dict[str, str] = {}
        for k, v in self.headers.items():
            if k.lower() in _HOP_BY_HOP:
                continue
            fwd_headers[k] = v
        # 注入 sticky key
        key = selector.current_key()
        fwd_headers["Authorization"] = f"Bearer {key}"

        logger.info("→ %s %s (sticky key=%s...)", method, path, key[:10])

        # 发送到上游（流式）
        try:
            with httpx.stream(method, url, headers=fwd_headers, content=body,
                              timeout=timeout) as r:
                # 判断成功/失败（用于 sticky 状态机）
                ok = 200 <= r.status_code < 400
                # 透传响应头
                resp_headers: list[tuple[str, str]] = []
                for k, v in r.headers.items():
                    if k.lower() in _RESP_STRIP:
                        continue
                    resp_headers.append((k, v))
                # 写状态行 + 头
                self.send_response(r.status_code)
                for k, v in resp_headers:
                    self.send_header(k, v)
                # 流式 body：逐 chunk 转发
                # 用 chunked 或直接写（不设 Content-Length，连接 close 时客户端能处理）
                # 这里不设 Content-Length，让 BaseHTTPRequestHandler 走 close 模式
                self.end_headers()
                try:
                    for chunk in r.iter_bytes():
                        if chunk:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    logger.warning("客户端断开连接（流式转发中止）")
                # 更新 sticky 状态（429 与其他失败统一按失败计数处理）
                if ok:
                    selector.on_success()
                else:
                    selector.on_failure(r.status_code)
                    logger.warning("← %d %s (sticky 计失败, key=%s...)",
                                   r.status_code, path, key[:10])
                    # 全部 key 被禁用 → 关闭服务退出程序
                    if selector.is_all_disabled():
                        self._shutdown_server()
        except httpx.TimeoutException:
            logger.error("⏱ 上游超时: %s %s", method, url)
            selector.on_failure(None)
            self._send_error(504, "upstream timeout")
            if selector.is_all_disabled():
                self._shutdown_server()
        except httpx.ConnectError as e:
            logger.error("🔗 上游连接失败: %s %s: %s", method, url, e)
            selector.on_failure(None)
            self._send_error(502, f"upstream connect error: {e}")
            if selector.is_all_disabled():
                self._shutdown_server()
        except Exception as e:
            logger.exception("转发异常: %s %s: %s", method, url, e)
            selector.on_failure(None)
            self._send_error(502, f"proxy error: {e}")
            if selector.is_all_disabled():
                self._shutdown_server()

    def _shutdown_server(self) -> None:
        """关闭 HTTP 服务（从请求线程调用 server.shutdown，需在独立线程避免死锁）。"""
        try:
            srv = self.server
            # shutdown() 会阻塞直到 serve_forever 返回，必须在独立线程调用
            threading.Thread(target=srv.shutdown, daemon=True).start()
        except Exception as e:
            logger.warning("关闭服务失败: %s", e)

    def _send_error(self, code: int, msg: str) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": msg, "code": code}}).encode())
        except Exception:
            pass

    # ---- 状态查询端点 ----
    def _send_status(self) -> None:
        selector: StickyKeySelector = self.server.selector  # type: ignore[attr-defined]
        body = json.dumps(selector.status(), ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- HTTP 方法 ----
    def do_GET(self) -> None:
        if self.path == "/sticky/status":
            self._send_status()
            return
        self._proxy("GET")

    def do_POST(self) -> None:
        self._proxy("POST")

    def do_PUT(self) -> None:
        self._proxy("PUT")

    def do_DELETE(self) -> None:
        self._proxy("DELETE")

    def do_PATCH(self) -> None:
        self._proxy("PATCH")

    def do_OPTIONS(self) -> None:
        self._proxy("OPTIONS")


def load_keys(keys_path: str | Path) -> tuple[list[str], set[int], dict[int, str]]:
    """从 keys.json 加载 Fireworks key 列表、已禁用下标集合、以及下标→email 映射。

    keys.json 由 state_db.export_keys 生成，每条含
    {"email","api_key",...,"disabled": bool}。
    disabled=True 的 key 仍保留在列表中（保留下标稳定），但其下标加入 disabled 集合，
    sticky_proxy 启动时预填充 StickyKeySelector.disabled，不再选用。

    下标→email 映射用于运行时永久禁用时回写 state.db（set_key_disabled 按 email 定位行），
    确保 DB 的 key_disabled 与 keys.json 的 disabled 实时一致，重启后不丢失禁用状态。

    返回 (keys, disabled_indexes, index_to_email)。
    """
    data = json.loads(Path(keys_path).read_text(encoding="utf-8"))
    keys: list[str] = []
    disabled: set[int] = set()
    index_to_email: dict[int, str] = {}
    for i, r in enumerate(data):
        if not isinstance(r, dict):
            continue
        k = (r.get("api_key") or "").strip()
        if not k:
            continue
        keys.append(k)
        idx = len(keys) - 1
        email = (r.get("email") or "").strip()
        if email:
            index_to_email[idx] = email
        if r.get("disabled"):
            disabled.add(idx)
    return keys, disabled, index_to_email


def main() -> int:
    ap = argparse.ArgumentParser(description="Fireworks sticky 转发代理（同 key 优先 + 连续失败 N 次永久禁用该 key）")
    here = Path(__file__).resolve().parent
    ap.add_argument("--keys", default=str(here.parent / "data" / "keys.json"), help="keys.json 路径")
    ap.add_argument("--state-db", default=str(here.parent / "data" / "state.db"),
                    help="state.db 路径（运行时永久禁用 key 时回写 key_disabled，"
                    "确保 DB 与 keys.json 同步；缺失则仅内存禁用，重启丢失）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，本机自用）")
    ap.add_argument("--port", type=int, default=3001, help="监听端口（默认 3001，避开 New API 3000）")
    ap.add_argument("--upstream", default="https://api.fireworks.ai/inference/v1",
                    help="Fireworks 上游端点（默认含 /v1）")
    ap.add_argument("--fail-threshold", type=int, default=3,
                    help="连续失败多少次后永久禁用该 key 并切换（默认 3，429 同样计入；全禁用时重置轮换）")
    ap.add_argument("--timeout", type=float, default=120.0, help="上游超时秒（默认 120）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    # 日志系统：控制台输出 + 保存到文件（最多留 3 份），Windows GBK 终端兼容内置
    setup_console_logging(
        log_dir=_PROJECT_ROOT / "data" / "logs",
        max_files=3,
        verbose=args.verbose,
    )
    # Windows GBK 终端 emoji 兼容（log_system 已 reconfigure，此处兜底）
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    keys, disabled, index_to_email = load_keys(args.keys)
    if not keys:
        logger.error("❌ keys.json 无有效 key: %s", args.keys)
        return 1
    # 构造 StateDB 句柄：运行时永久禁用 key 时回写 DB key_disabled + 重新 export keys.json，
    # 避免"运行期禁用只存内存、重启丢失、DB 与 keys.json 不同步"。StateDB 缺失则降级纯内存。
    db = None
    if StateDB is not None and Path(args.state_db).exists():
        try:
            db = StateDB(args.state_db)
            logger.info("🗄️ 已连接 state.db: %s（运行时禁用将回写 DB + keys.json）",
                        args.state_db)
        except Exception as e:
            logger.warning("⚠️ 打开 state.db 失败(%s): %s，降级为纯内存禁用（重启将丢失）",
                           args.state_db, e)
            db = None
    elif StateDB is None:
        logger.warning("⚠️ state_db 模块不可用（registrar/ 缺失），降级为纯内存禁用（重启将丢失）")
    else:
        logger.warning("⚠️ state.db 不存在: %s，降级为纯内存禁用（重启将丢失）", args.state_db)
    selector = StickyKeySelector(
        keys, fail_threshold=args.fail_threshold,
        index_to_email=index_to_email, db=db, keys_json_path=args.keys,
    )
    # 预填充 DB 中已标记永久禁用的 key（keys.json 的 disabled 字段）
    if disabled:
        selector.disabled = set(disabled)
        # 当前 key 若被预禁用，切到第一个可用；全禁用则置 all_disabled（不再重置兜底）
        avail = selector._next_available(selector.current_index)
        if avail is None:
            # 启动时即全部 suspend：报错并退出程序（不再用坏 key 重置轮换）
            selector.all_disabled = True
            logger.critical("🚫 keys.json 中所有 %d 个 key 均已禁用（全部 suspend），"
                            "无可用 key，退出程序", len(keys))
            return 1
        else:
            selector.current_index = avail
        logger.info("🚫 预禁用 %d 个 key（disabled 字段）: %s",
                    len(disabled), sorted(disabled))
    logger.info("🔑 加载 %d 个 Fireworks key（可用 %d，禁用 %d），sticky 阈值=%d",
                len(keys), len(keys) - len(selector.disabled),
                len(selector.disabled), args.fail_threshold)
    logger.info("▶ sticky 代理启动: http://%s:%d/v1/* → %s/*", args.host, args.port, args.upstream)
    logger.info("  状态查询: GET http://%s:%d/sticky/status", args.host, args.port)
    logger.info("  当前 sticky key: %s...", selector.current_key()[:10])

    server = ThreadingHTTPServer((args.host, args.port), StickyProxyHandler)
    server.selector = selector  # type: ignore[attr-defined]
    server.upstream = args.upstream.rstrip("/")  # type: ignore[attr-defined]
    server.timeout_upstream = args.timeout  # type: ignore[attr-defined]

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("🛑 收到中断，关闭 sticky 代理")
    finally:
        server.server_close()
    # 运行中所有 key 被禁用 → 报错退出程序（exit 1，不再用坏 key 兜底）
    if selector.is_all_disabled():
        logger.critical("🚫 运行中所有 key 均已被禁用（全部 suspend），无可用 key，退出程序")
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

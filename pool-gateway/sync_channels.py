"""sync_channels.py — 批量把 Fireworks Key 录入 New API 渠道。

读取 registrar 产出的 keys.json，对每个 key 调 New API：
    POST /api/channel/  (session cookie + New-Api-User header)
New API 内置：加权随机选渠道 + 失败自动重试/禁用 + 统一 /v1/chat/completions。

New API 认证方式（实测 v1.0.0-rc.14）：管理 API（/api/channel/）需 session cookie
（POST /api/user/login 获取）+ New-Api-User header（user_id）。access_token 仅用于
对外 /v1/chat/completions 调用，不能调管理 API。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# 项目根在 pool-gateway/ 上一级，加入 sys.path 以便 import log_system
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from log_system import setup_console_logging  # noqa: E402

# New API 渠道类型常量（1 = OpenAI 兼容）
CHANNEL_TYPE_OPENAI = 1


class NewAPIClient:
    """New API 管理客户端（session cookie 认证）。"""

    def __init__(self, base_url: str, admin_user: str, admin_pass: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base_url, timeout=30)
        # login 拿 session cookie + user_id
        r = self.client.post(
            "/api/user/login",
            json={"username": admin_user, "password": admin_pass},
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"New API 登录失败: {data.get('message')}")
        self.user_id = str(data["data"]["id"])
        # httpx 自动管理 cookie（session 已存入 client.cookies）
        self.headers = {"New-Api-User": self.user_id, "Content-Type": "application/json"}
        logger.info("New API 登录成功（user_id=%s），已获取 session", self.user_id)

    def add_channel(self, payload: dict) -> dict:
        r = self.client.post("/api/channel/", json=payload, headers=self.headers)
        r.raise_for_status()
        return r.json()

    def list_channels(self, page: int = 1, page_size: int = 100) -> dict:
        r = self.client.get("/api/channel/", params={"p": page, "page_size": page_size}, headers=self.headers)
        r.raise_for_status()
        return r.json()

    def delete_channel(self, channel_id: int) -> dict:
        r = self.client.delete(f"/api/channel/{channel_id}", headers=self.headers)
        r.raise_for_status()
        return r.json()

    def test_channel(self, channel_id: int) -> dict:
        """测试渠道可用性。"""
        r = self.client.get(f"/api/channel/test/{channel_id}", headers=self.headers)
        r.raise_for_status()
        return r.json()


def build_channel_payload(key_rec: dict, models: list[str], fireworks_base_url: str,
                          group: str, priority: int, weight: int,
                          channel_headers: str = "") -> dict:
    """构造单个渠道的 AddChannel payload。

    New API v1.0.0-rc.14 实测（context7 确认）：AddChannel 需嵌套结构
    {mode: "single", channel: {...}}，且字段是 groups（数组）不是 group（字符串）。
    旧版扁平 payload + group 字符串会触发 channel.go:942 空指针 panic。

    自定义标头转发（确保所有 Fireworks 接口能正常转发）：
    - `header` 字段：New API 渠道级"自定义请求头"，JSON 字符串，转发时**附加**到上游 Fireworks 请求。
      用于 Fireworks 需要的固定特殊标头（如 X-Fireworks-* / 自定义追踪标头）。
    - 客户端请求自带的任意标头：New API 默认透传到上游（Authorization 被渠道 key 替换，
      hop-by-hop 头除外）。因此客户端临时自定义标头无需配置即可转发。
    - 两者叠加：渠道固定标头（header 字段）+ 客户端临时标头（默认透传）全覆盖。
    """
    email = key_rec.get("email", "unknown")
    # 渠道 payload 不含任何价格/配额/余额字段（OpenAI 兼容渠道无 model_ratio/remain_quota 等）。
    # 配额限制在 token 层（start.py 创建 token 时 unlimited_quota=True），渠道层天然无限转发。
    # 即"删除模型价格/已用/剩余"在渠道层无需额外处理：渠道只做透传，不感知也不限制上游额度。
    channel = {
        "name": f"fw-{email}",
        "type": CHANNEL_TYPE_OPENAI,
        "base_url": fireworks_base_url,
        "key": key_rec["api_key"],
        "models": ",".join(models),
        "groups": [group],          # 新版是数组
        "group": group,             # 兼容旧版
        "priority": priority,
        "weight": weight,
        "model_mapping": "",
        "status": 1,                # 1=启用
        "auto_ban": 1,              # 失败自动禁用
        # 渠道级自定义请求头（JSON 字符串）。New API 转发时附加到上游 Fireworks 请求。
        # 例：{"X-Fireworks-Gen-Random-Seed":"42"}。留空则不附加。
        "header": channel_headers,
    }
    return {"mode": "single", "channel": channel}


def sync(keys_path: str | Path, models_path: str | Path, env_path: str | Path | None = None) -> int:
    """同步 keys 到 New API 渠道。返回成功录入数。"""
    # 加载环境
    if env_path and Path(env_path).exists():
        load_dotenv(env_path)
    base_url = os.getenv("NEWAPI_BASE_URL", "http://127.0.0.1:3000")
    admin_user = os.getenv("NEWAPI_ADMIN_USER", "root")
    admin_pass = os.getenv("NEWAPI_ADMIN_PASS", "")
    # 渠道 base_url：去掉末尾 /v1（New API 自动补 /v1/chat/completions，否则双重 /v1 报 404）
    fireworks_base_url = os.getenv("FIREWORKS_BASE_URL", "https://api.fireworks.ai/inference")
    group = os.getenv("CHANNEL_GROUP", "default")
    priority = int(os.getenv("CHANNEL_PRIORITY", "0"))
    weight = int(os.getenv("CHANNEL_WEIGHT", "100"))
    # 渠道级自定义请求头（JSON 字符串，转发时附加到上游 Fireworks）。
    # 确保所有 Fireworks 接口（含特殊标头）都能正常转发。留空则不附加。
    # 例：FIREWORKS_CHANNEL_HEADERS={"X-Fireworks-Gen-Random-Seed":"42","X-Custom":"abc"}
    channel_headers = os.getenv("FIREWORKS_CHANNEL_HEADERS", "")
    # 校验 JSON 格式（非空时）
    if channel_headers.strip():
        try:
            json.loads(channel_headers)
        except json.JSONDecodeError as e:
            logger.error("FIREWORKS_CHANNEL_HEADERS 不是合法 JSON: %s", e)
            return 0

    if not admin_pass:
        logger.error("NEWAPI_ADMIN_PASS 未配置，请在 newapi.env 填入管理员密码（用于 login 拿 session）")
        return 0

    keys = json.loads(Path(keys_path).read_text(encoding="utf-8"))
    models_data = json.loads(Path(models_path).read_text(encoding="utf-8"))
    models = models_data.get("models", [])
    if not models:
        logger.error("models.json 模型列表为空")
        return 0
    if not keys:
        logger.info("keys.json 为空，无 Key 可同步")
        return 0

    # 统计可用/禁用 key；若全部禁用（全部 suspend）则报错退出
    usable = [r for r in keys if not r.get("disabled")]
    disabled_count = len(keys) - len(usable)
    if not usable:
        logger.critical("🚫 所有 %d 个 key 均已禁用（全部 suspend），无可用 key，退出程序", len(keys))
        return -1
    logger.info("keys.json：%d 个 key（可用 %d，禁用 %d）", len(keys), len(usable), disabled_count)

    client = NewAPIClient(base_url, admin_user, admin_pass)
    # 先取已存在渠道名集合，避免重复录入
    existing_names: set[str] = set()
    try:
        resp = client.list_channels(page=1, page_size=1000)
        # 新版 data 结构：{"items":[...]} 或旧版 data 直接是 list
        items = resp.get("data", {})
        if isinstance(items, dict):
            items = items.get("items", []) or []
        for ch in items:
            existing_names.add(ch.get("name", ""))
    except Exception as e:
        logger.warning("获取已有渠道失败（继续尝试录入）: %s", e)

    success = 0
    skipped_disabled = 0
    for rec in keys:
        email = rec.get("email", "unknown")
        name = f"fw-{email}"
        # 跳过被永久禁用的 key（DB key_disabled=1 → keys.json disabled=true），
        # 避免把失效 key 录入号池导致调用失败
        if rec.get("disabled"):
            skipped_disabled += 1
            logger.info("跳过已禁用 key 渠道 %s（disabled=true）", name)
            continue
        if name in existing_names:
            logger.info("跳过已存在渠道 %s", name)
            continue
        payload = build_channel_payload(rec, models, fireworks_base_url, group, priority, weight,
                                        channel_headers=channel_headers)
        try:
            resp = client.add_channel(payload)
            if resp.get("success"):
                success += 1
                logger.info("✅ 录入渠道 %s（key 前 8 位: %s...）", name, rec["api_key"][:8])
            else:
                logger.error("❌ 录入失败 %s: %s", name, resp.get("message"))
        except Exception as e:
            logger.error("❌ 录入异常 %s: %s", name, e)
    logger.info("同步完成：成功 %d / 跳过禁用 %d / 总计 %d", success, skipped_disabled, len(keys))
    return success


def main() -> int:
    ap = argparse.ArgumentParser(description="同步 Fireworks Key 到 New API 渠道")
    here = Path(__file__).resolve().parent
    ap.add_argument("--keys", default=str(here.parent / "data" / "keys.json"), help="keys.json 路径")
    ap.add_argument("--models", default=str(here / "models.json"), help="models.json 路径")
    ap.add_argument("--env", default=str(here / "newapi.env"), help="newapi.env 路径")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    # 日志系统：控制台输出 + 保存到文件（最多留 3 份）
    setup_console_logging(
        log_dir=_PROJECT_ROOT / "data" / "logs",
        max_files=3,
        verbose=args.verbose,
    )
    n = sync(args.keys, args.models, args.env)
    return 0 if n >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())

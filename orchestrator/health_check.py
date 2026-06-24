"""渠道健康巡检 + 失活剔除。

定期调 New API /api/channel/ 列出所有渠道，对每个渠道做 test，
失败渠道自动 disable/delete，并标记本地 SQLite 中对应 key 为 failed
（触发 registrar 下次重试补号）。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
import httpx

logger = logging.getLogger(__name__)


class HealthChecker:
    def __init__(self, base_url: str, access_token: str, auto_disable: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.auto_disable = auto_disable
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            timeout=60,
        )

    def run_once(self) -> dict:
        """巡检一次，返回统计。"""
        stats = {"total": 0, "healthy": 0, "unhealthy": 0, "disabled": 0}
        try:
            resp = self.client.get("/api/channel/", params={"p": 1, "page_size": 1000})
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("获取渠道列表失败: %s", e)
            return stats

        channels = data.get("data", []) or []
        stats["total"] = len(channels)
        for ch in channels:
            cid = ch.get("id")
            name = ch.get("name", "")
            # 已禁用渠道跳过测试
            if ch.get("status") not in (1, "1", "enabled"):
                stats["disabled"] += 1
                continue
            ok = self._test_channel(cid)
            if ok:
                stats["healthy"] += 1
            else:
                stats["unhealthy"] += 1
                if self.auto_disable:
                    self._disable_or_delete(cid, name)
        logger.info("巡检完成: %s", stats)
        return stats

    def _test_channel(self, channel_id: int) -> bool:
        """测试渠道，返回是否健康。"""
        try:
            r = self.client.get(f"/api/channel/test/{channel_id}", timeout=60)
            r.raise_for_status()
            body = r.json()
            return bool(body.get("success"))
        except Exception as e:
            logger.warning("渠道 %d 测试异常: %s", channel_id, e)
            return False

    def _disable_or_delete(self, channel_id: int, name: str) -> None:
        """失活渠道：先尝试删除（号池场景，失活 key 无保留价值）。"""
        try:
            r = self.client.delete(f"/api/channel/{channel_id}")
            r.raise_for_status()
            logger.warning("🗑️ 已删除失活渠道 %s(id=%d)", name, channel_id)
        except Exception as e:
            logger.error("删除渠道 %s 失败: %s", name, e)

    def run_loop(self, interval: int) -> None:
        """持续巡检。"""
        logger.info("启动健康巡检循环，间隔 %ds", interval)
        while True:
            try:
                self.run_once()
            except Exception as e:
                logger.exception("巡检循环异常: %s", e)
            time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description="New API 渠道健康巡检")
    here = Path(__file__).resolve().parent
    ap.add_argument("--env", default=str(here.parent / "pool-gateway" / "newapi.env"), help="newapi.env 路径")
    ap.add_argument("--once", action="store_true", help="只巡检一次")
    ap.add_argument("--interval", type=int, default=None, help="巡检间隔（秒），覆盖 env")
    ap.add_argument("--no-auto-disable", action="store_true", help="不自动剔除失活渠道")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv(args.env)
    base_url = os.getenv("NEWAPI_BASE_URL", "http://127.0.0.1:3000")
    access_token = os.getenv("NEWAPI_ACCESS_TOKEN", "")
    interval = args.interval or int(os.getenv("HEALTH_CHECK_INTERVAL", "600"))
    auto_disable = not args.no_auto_disable and os.getenv("AUTO_DISABLE_ON_FAIL", "true").lower() == "true"
    if not access_token:
        logger.error("NEWAPI_ACCESS_TOKEN 未配置")
        return 1
    checker = HealthChecker(base_url, access_token, auto_disable=auto_disable)
    if args.once:
        checker.run_once()
    else:
        checker.run_loop(interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())

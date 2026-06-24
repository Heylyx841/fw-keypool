"""一键流水线：造号 → 同步渠道 → 健康检查 → 库存告警。

串联 registrar + sync_channels + health_check + alerts，
实现"一条命令养号 + 上号池 + 巡检"闭环。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
REGISTRAR = ROOT / "registrar"
POOL = ROOT / "pool-gateway"
ORCH = ROOT / "orchestrator"


def _run(cmd: list[str], cwd: Path, label: str) -> bool:
    """运行子进程，返回是否成功。"""
    logger.info("▶ %s: %s（cwd=%s）", label, " ".join(cmd), cwd)
    try:
        r = subprocess.run(cmd, cwd=str(cwd), check=False)
        if r.returncode != 0:
            logger.error("✗ %s 失败（exit %d）", label, r.returncode)
            return False
        logger.info("✓ %s 完成", label)
        return True
    except Exception as e:
        logger.exception("%s 异常: %s", label, e)
        return False


async def run_pipeline(limit: int | None = None, skip_register: bool = False,
                       skip_sync: bool = False, skip_health: bool = False,
                       threshold: int = 3) -> int:
    """执行完整流水线，返回 0=成功。"""
    steps_ok = True

    # 1. 造号
    if not skip_register:
        cmd = [sys.executable, "run.py"]
        if limit:
            cmd += ["--limit", str(limit)]
        # 需在 registrar 目录跑（导入本地模块）
        ok = _run(cmd, REGISTRAR, "造号 registrar")
        steps_ok = steps_ok and ok

    # 2. 同步渠道
    if not skip_sync:
        ok = _run([sys.executable, "sync_channels.py"], POOL, "同步渠道 sync_channels")
        steps_ok = steps_ok and ok

    # 3. 健康巡检（单次）
    if not skip_health:
        ok = _run([sys.executable, "health_check.py", "--once"], ORCH, "健康巡检 health_check")
        steps_ok = steps_ok and ok

    # 4. 库存告警
    ok = _run([sys.executable, "alerts.py", "--threshold", str(threshold)], ORCH, "库存告警 alerts")
    # 告警返回 1 不算流水线失败

    return 0 if steps_ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="fw-keypool 一键流水线")
    ap.add_argument("--limit", type=int, default=None, help="造号上限")
    ap.add_argument("--skip-register", action="store_true")
    ap.add_argument("--skip-sync", action="store_true")
    ap.add_argument("--skip-health", action="store_true")
    ap.add_argument("--threshold", type=int, default=3, help="库存告警阈值")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    return asyncio.run(run_pipeline(
        limit=args.limit,
        skip_register=args.skip_register,
        skip_sync=args.skip_sync,
        skip_health=args.skip_health,
        threshold=args.threshold,
    ))


if __name__ == "__main__":
    sys.exit(main())

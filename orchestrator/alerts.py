"""库存/失活告警。

检查本地 SQLite 中 done 状态 key 数量，低于阈值时输出告警。
巡检发现失活渠道也会触发。
可扩展为 webhook/邮件通知（当前仅日志 + 返回码）。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def check_stock(state_db_path: str | Path, threshold: int = 3) -> int:
    """检查号池库存，返回 0=正常 1=告警。"""
    # 延迟导入避免循环依赖
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "registrar"))
    from state_db import DONE, StateDB

    db = StateDB(state_db_path)
    done = db.list_jobs(status=DONE)
    n = len(done)
    if n < threshold:
        logger.warning("⚠️ 号池库存告警：可用 key 仅 %d（阈值 %d），建议补充", n, threshold)
        return 1
    logger.info("号池库存正常：可用 key %d（阈值 %d）", n, threshold)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="号池库存告警")
    here = Path(__file__).resolve().parent
    ap.add_argument("--db", default=str(here.parent / "data" / "state.db"), help="state.db 路径")
    ap.add_argument("--threshold", type=int, default=3, help="库存告警阈值")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    return check_stock(args.db, args.threshold)


if __name__ == "__main__":
    sys.exit(main())

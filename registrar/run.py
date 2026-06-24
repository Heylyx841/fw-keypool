"""registrar CLI 入口。

用法：
    python run.py                      # 全量运行
    python run.py --limit 5            # 只处理 5 个
    python run.py --load-only          # 仅加载邮箱池不运行
    python run.py --reset-failed       # 把 failed 重置为 pending
    python run.py --export-keys        # 仅导出 keys.json
    python run.py --alias-count 20     # Catch-all 邮箱别名展开数
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from config import load_config
from orchestrator import RegistrarOrchestrator

# 项目根在 registrar/ 上一级，把根加入 sys.path 以便 import log_system
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from log_system import setup_console_logging  # noqa: E402


def setup_logging(verbose: bool) -> None:
    """初始化日志：控制台输出 + 保存到文件（最多留 3 份）。"""
    setup_console_logging(
        log_dir=_PROJECT_ROOT / "data" / "logs",
        max_files=3,
        verbose=verbose,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="fw-keypool 造号端")
    ap.add_argument("--config", default=None, help="config.yaml 路径")
    ap.add_argument("--pool", default=None, help="邮箱池文件路径（默认读 .env EMAIL_POOL_FILE）")
    ap.add_argument("--limit", type=int, default=None, help="本次处理上限")
    ap.add_argument("--alias-count", type=int, default=0, help="Catch-all 别名展开数")
    ap.add_argument("--load-only", action="store_true", help="仅加载邮箱池入库")
    ap.add_argument("--reset-failed", action="store_true", help="重置 failed → pending")
    ap.add_argument("--export-keys", action="store_true", help="仅导出 keys.json")
    ap.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    args = ap.parse_args()

    setup_logging(args.verbose)
    cfg = load_config(args.config)
    orch = RegistrarOrchestrator(cfg)

    if args.reset_failed:
        n = orch.db.reset_failed_to_pending()
        print(f"已重置 {n} 个 failed → pending")
        return 0

    if args.export_keys:
        n = orch.db.export_keys(cfg.abs_path(cfg.paths.keys_json))
        print(f"已导出 {n} 个 key → {cfg.paths.keys_json}")
        return 0

    pool = args.pool or cfg.email_pool_file
    # 相对路径按项目根解析（而非 CWD），避免在 registrar/ 子目录运行时找不到上级的 emails.csv
    pool_path = cfg.abs_path(pool)
    if not pool_path.exists():
        print(f"邮箱池文件不存在: {pool_path}（CWD={__import__('os').getcwd()}）")
        return 1
    orch.load_pool_into_db(pool_path, alias_count=args.alias_count)

    if args.load_only:
        print("邮箱池已加载，未运行（--load-only）")
        return 0

    asyncio.run(orch.run(limit=args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())

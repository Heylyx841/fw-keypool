"""从登录开始的测试入口（跳过注册+验证，单独测登录→onboarding→保存登录态）。

用途：
- 注册流程已跑通但登录/onboarding 出问题时，单独复现登录链路，无需重跑整个注册。
- 验证密码持久化修复后，用 DB 中已存的密码能否成功登录。

用法：
    python login_test.py                           # 从 DB 取第一个有密码的 job
    python login_test.py --email user@example.com  # 指定邮箱（密码从 DB 读）
    python login_test.py --email user@example.com --password 'xxx'  # 手动指定密码
    python login_test.py --list                    # 列出 DB 中所有有密码的 job
    python login_test.py -v                        # 详细日志
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from config import load_config
from email_pool import EmailAccount
from fireworks_registrar import FireworksRegistrar, RegistrationResult
from state_db import StateDB


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Windows GBK 终端无法输出 emoji（✅❌），强制 stdout 用 utf-8
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def pick_job(db: StateDB, email: str | None):
    """从 DB 选取待测 job。指定 email 则取该邮箱，否则取第一个有密码的 job。"""
    if email:
        job = db.get_job(email)
        if not job:
            print(f"❌ DB 中无此邮箱: {email}")
            return None
        if not job.password:
            print(f"❌ 该邮箱在 DB 中无保存密码: {email}（无法用持久化密码登录）")
            return None
        return job
    # 未指定 email：取第一个有密码的 job
    for job in db.list_jobs():
        if job.password:
            return job
    print("❌ DB 中无任何带密码的 job，请先用 run.py 完成一次注册")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="fw-keypool 登录测试入口（从登录开始）")
    ap.add_argument("--config", default=None, help="config.yaml 路径")
    ap.add_argument("--email", default=None, help="指定测试邮箱（默认取 DB 第一个有密码的 job）")
    ap.add_argument("--password", default=None, help="手动指定密码（默认从 DB 读 job.password）")
    ap.add_argument("--list", action="store_true", help="列出 DB 中所有带密码的 job 后退出")
    ap.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    args = ap.parse_args()

    setup_logging(args.verbose)
    cfg = load_config(args.config)
    db = StateDB(cfg.abs_path(cfg.paths.state_db))

    if args.list:
        jobs = [j for j in db.list_jobs() if j.password]
        if not jobs:
            print("DB 中无带密码的 job")
            return 0
        print(f"共 {len(jobs)} 个带密码的 job：")
        for j in jobs:
            print(f"  {j.email}  status={j.status}  password={j.password}  auth_code={j.auth_code}  retry={j.retry_count}")
        return 0

    job = pick_job(db, args.email)
    if not job:
        return 1

    password = args.password or job.password
    src = "(DB)" if not args.password else "(手动)"
    # 代理：从 proxy_pool 随机选（与 run.py 一致），降低封 IP 风险
    import random as _rand
    proxy = _rand.choice(cfg.registrar.proxy_pool) if cfg.registrar.proxy_pool else None
    print(f"测试登录：email={job.email} password{src}={password}")
    if proxy:
        print(f"  代理：{proxy}")
    print(f"  若登录 Invalid，说明该邮箱在 Fireworks 端的实际密码 ≠ DB 密码（注册时 fill 未生效）。")
    print(f"  可用 --password 手动指定正确密码验证 onboarding 链路。")

    registrar = FireworksRegistrar(cfg)
    storage_state = str(cfg.abs_path(cfg.paths.screenshots_dir) / f"state_{job.email}.json")

    async def _run() -> RegistrationResult:
        return await registrar.login_and_onboard(
            account=EmailAccount(
                email=job.email,
                protocol=job.protocol,
                host=job.host,
                port=job.port,
                username=job.username,
                auth_code=job.auth_code,
                base_email=job.base_email,
            ),
            password=password,
            proxy=proxy,
            storage_state_path=storage_state,
        )

    result = asyncio.run(_run())
    if result.success:
        print(f"\n✅ 登录+onboarding 成功：{job.email}")
        print(f"   登录态已保存：{storage_state}")
        if result.error:
            print(f"   注意：{result.error}")
        return 0
    print(f"\n❌ 失败：{job.email}")
    print(f"   错误：{result.error}")
    return 2


if __name__ == "__main__":
    sys.exit(main())

"""注册编排器：并发控制 + 断点续跑 + 失败重试。

职责：
- 从邮箱池加载邮箱 → 写入 SQLite（已完成的不重复）
- 并发调度 FireworksRegistrar + KeyFetcher
- 每个邮箱一个 job，状态机驱动，失败重试，断点续跑
- 产出 keys.json 供 sync_channels 消费
"""
from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path

from config import AppConfig
from email_pool import EmailAccount, load_email_pool
from fireworks_registrar import FireworksRegistrar, RegistrationResult
from karing_proxy import KaringProxyController
from key_fetcher import KeyFetcher, KeyFetchResult
from state_db import (
    DONE,
    EMAIL_VERIFYING,
    FAILED,
    FETCHING_KEY,
    REGISTERING,
    StateDB,
    VERIFIED,
)

logger = logging.getLogger(__name__)


class RegistrarOrchestrator:
    """造号编排器。"""

    def __init__(self, app_config: AppConfig) -> None:
        self.cfg = app_config
        self.db = StateDB(app_config.abs_path(app_config.paths.state_db))
        self.registrar = FireworksRegistrar(app_config)
        self.key_fetcher = KeyFetcher(app_config)
        self.screenshots_dir = app_config.abs_path(app_config.paths.screenshots_dir)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        # Karing 代理控制器：每注册一个 key 切换一个延迟 <400ms 的可用出口节点
        kc = app_config.karing
        self.karing: KaringProxyController | None = None
        if kc.enabled:
            self.karing = KaringProxyController(
                api_host=kc.api_host,
                api_port=kc.api_port,
                proxy_port=kc.proxy_port,
                dashboard_port=kc.dashboard_port,
                cluster_port=kc.cluster_port,
                selector_name=kc.selector_name,
                secret=kc.secret,
                delay_test_url=kc.delay_test_url,
                max_latency_ms=kc.max_latency_ms,
                node_timeout_ms=kc.node_timeout_ms,
                switch_per_key=kc.switch_per_key,
                request_timeout=kc.request_timeout,
            )

    def load_pool_into_db(self, pool_file: str | Path, alias_count: int = 0) -> int:
        """加载邮箱池写入 SQLite（已 done 的跳过）。返回新增数。"""
        accounts = load_email_pool(pool_file, alias_count=alias_count)
        added = 0
        for acc in accounts:
            existing = self.db.get_job(acc.email)
            if existing and existing.status == DONE:
                continue
            if existing is None:
                self.db.upsert_email(
                    email=acc.email,
                    protocol=acc.protocol,
                    host=acc.host,
                    port=acc.port,
                    username=acc.username,
                    auth_code=acc.auth_code,
                    base_email=acc.base_email,
                )
                added += 1
        logger.info("邮箱池加载完成：新增 %d，总计 %d", added, len(accounts))
        return added

    async def run(self, limit: int | None = None) -> None:
        """运行造号流水线。"""
        pending = self.db.list_pending(limit=limit or 999999)
        if not pending:
            logger.info("无待处理任务")
            return
        logger.info("待处理任务 %d，并发 %d", len(pending), self.cfg.registrar.concurrency)
        sem = asyncio.Semaphore(self.cfg.registrar.concurrency)

        async def _wrap(job):
            async with sem:
                # 并发间随机延时，降低风控
                delay = random.uniform(self.cfg.registrar.min_delay_sec, self.cfg.registrar.max_delay_sec)
                await asyncio.sleep(delay)
                await self._process_job(job)

        await asyncio.gather(*[_wrap(j) for j in pending])
        # 导出 keys
        n = self.db.export_keys(self.cfg.abs_path(self.cfg.paths.keys_json))
        logger.info("流水线结束，导出 %d 个 key", n)

    async def _process_job(self, job) -> None:
        """处理单个邮箱 job：注册 → 取 Key，状态机驱动。

        密码持久化：首次注册生成随机密码并存 DB；重试时从 DB 读同一密码传给 register，
        保证跨重试的注册密码与登录密码一致（修复"重试密码不一致→登录 Invalid"问题）。
        """
        # Karing：每注册一个 key 前切换到一个延迟 <400ms 的可用出口节点（换 IP 降封）
        karing_node: str | None = None
        karing_latency: int | None = None
        if self.karing is not None:
            try:
                karing_node, karing_latency = await self.karing.pick_fast_unused_node()
                if karing_node:
                    logger.info("🔑 [%s] karing 已切换出口节点=%s 延迟=%dms",
                                job.email, karing_node, karing_latency)
                else:
                    logger.warning("⚠️ [%s] karing 无可用节点(<%dms)，沿用当前出口",
                                   job.email, self.karing.max_latency_ms)
            except Exception as e:
                logger.warning("⚠️ [%s] karing 切换节点异常（不阻断，沿用当前出口）: %s",
                               job.email, e)

        proxy = random.choice(self.cfg.registrar.proxy_pool) if self.cfg.registrar.proxy_pool else None
        storage_state = str(self.screenshots_dir / f"state_{job.email}.json")

        # 注册阶段：复用 DB 中已有密码（重试时），无则由 register 生成
        existing_password = job.password or None
        self.db.update_status(job.email, REGISTERING, proxy_used=proxy)
        # POP 登录失败回调：累加 DB 计数，返回当前值供 fetch_verification 判定假邮箱阈值
        def _on_pop_login_fail() -> int:
            return self.db.incr_pop_fail(job.email)
        reg: RegistrationResult = await self.registrar.register(
            account=EmailAccount(
                email=job.email,
                protocol=job.protocol,
                host=job.host,
                port=job.port,
                username=job.username,
                auth_code=job.auth_code,
                base_email=job.base_email,
            ),
            password=existing_password,
            proxy=proxy,
            storage_state_path=storage_state,
            on_pop_login_fail=_on_pop_login_fail,
        )
        # 无论成功失败，都把本次用的密码存入 DB（首次生成后持久化，重试复用）
        if reg.password:
            self.db.update_status(job.email, REGISTERING, password=reg.password)
        if not reg.success:
            # 假邮箱判定：POP 登录连续失败超阈值 → 永久禁用该邮箱，不再重试
            if reg.is_fake_email:
                self.db.set_email_disabled(job.email, True)
                self.db.update_status(job.email, FAILED, error=reg.error)
                logger.error("🚫 假邮箱已禁用 %s（POP 登录连续失败超阈值，不再重试）: %s",
                             job.email, reg.error)
                return
            self._fail(job, reg.error, incr_retry=True)
            return
        self.db.update_status(
            job.email,
            EMAIL_VERIFYING,
            fireworks_user_id=reg.fireworks_user_id,
            password=reg.password,
        )
        # 注册阶段已含邮件验证，直接进 verified
        self.db.update_status(job.email, VERIFIED)

        # 取 Key 阶段
        self.db.update_status(job.email, FETCHING_KEY)
        kf: KeyFetchResult = await self.key_fetcher.fetch_key(storage_state, proxy=proxy)
        if not kf.success or not kf.api_key:
            self._fail(job, f"取 Key 失败: {kf.error}", incr_retry=True)
            return

        self.db.update_status(job.email, DONE, api_key=kf.api_key)
        logger.info("✅ 完成 %s", job.email)

    def _fail(self, job, error: str | None, incr_retry: bool) -> None:
        """记录失败，超过重试上限则标 failed，否则回 pending 等下次重试。"""
        retry = job.retry_count + (1 if incr_retry else 0)
        if retry >= self.cfg.registrar.max_retry:
            self.db.update_status(job.email, FAILED, error=error)
            logger.error("❌ 失败 %s（已达重试上限 %d）: %s", job.email, retry, error)
        else:
            # 回 pending 等下次 run 重试
            self.db.update_status(job.email, "pending", error=error, incr_retry=incr_retry)
            logger.warning("⚠️ 失败 %s（第 %d 次，将重试）: %s", job.email, retry, error)

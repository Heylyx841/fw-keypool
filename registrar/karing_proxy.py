"""Karing 代理控制器：每注册一个 key 换一个可用（延迟 <400ms）的代理节点。

Karing 是 Clash 兼容（基于 sing-box 核心）的图形代理客户端：
- 控制端口（默认 3057）：暴露 Clash 风格 RESTful API，可列节点 / 切换 selector / 测延迟
- 代理出口端口（默认 3067）：Playwright/HTTP 请求走这里，karing 内部按当前选中节点转发
- 在线面板端口（默认 3072）：Web 面板（非核心，仅记录/可探活）
- 集群服务端口（默认 3050）：集群 API（非核心，仅记录）

本模块通过控制端口 API 实现「每注册一个 key 换一个延迟 <400ms 的节点」：
1. GET /proxies 列出所有代理组与节点
2. 自动找 Selector 类型组（可手动切换；URLTest 是自动选最快不可 PUT 切换）
3. GET /proxies/{node}/delay?url=...&timeout=... 测单节点延迟（返回 {delay: ms}）
4. PUT /proxies/{selector} {"name": nodeX} 切换当前出口节点
5. 轮换策略：记录已用节点，优先选未用过的可用节点，全用完则重置重来

依赖：httpx（已在 registrar 依赖中）。无新依赖。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Clash RESTful API（karing 兼容）端点
_EP_PROXIES = "/proxies"  # 列所有代理/组
_EP_PROXY = "/proxies/{name}"  # 单代理详情 / PUT 切换 selector
_EP_DELAY = "/proxies/{name}/delay"  # 测单节点延迟

# Selector 类型可手动 PUT 切换；URLTest/Fallback 是自动选路，sing-box 对其 PUT 返回 400 "Must be a Selector"
_SWITCHABLE_TYPES = ("Selector",)
# 可列节点的组类型（含自动选路组，用于兜底列节点 + 测延迟做可用性验证）
_GROUP_TYPES = ("Selector", "URLTest", "Fallback", "LoadBalance")


class KaringProxyError(RuntimeError):
    """Karing 控制接口异常。"""


class KaringProxyController:
    """通过 karing 控制端口(3057) 切换代理出口节点，保证每次注册走一个可用（<400ms）的新节点。

    线程/协程安全：内部用 asyncio.Lock 保护「已用节点集合 + 当前选择」状态，
    多个 job 并发调用 pick_fast_unused_node 不会竞态。
    """

    def __init__(
        self,
        api_host: str = "127.0.0.1",
        api_port: int = 3057,
        proxy_port: int = 3067,
        dashboard_port: int = 3072,
        cluster_port: int = 3050,
        selector_name: str = "",
        secret: str = "",
        delay_test_url: str = "https://www.gstatic.com/generate_204",
        max_latency_ms: int = 400,
        node_timeout_ms: int = 5000,
        switch_per_key: bool = True,
        request_timeout: float = 6.0,
    ) -> None:
        self.api_base = f"http://{api_host}:{api_port}"
        self.proxy_out = f"http://{api_host}:{proxy_port}"  # 出口代理（Playwright 用）
        self.dashboard_url = f"http://{api_host}:{dashboard_port}"
        self.cluster_url = f"http://{api_host}:{cluster_port}"
        self.selector_name = selector_name.strip()
        # karing 控制端口 secret 认证（见 karing service.json "secret" 字段）。
        # 无 secret 时控制 API 返回 401。带 secret 的请求附加 Authorization: Bearer <secret>。
        self.secret = secret.strip()
        self._auth_headers = {"Authorization": f"Bearer {self.secret}"} if self.secret else None
        self.delay_test_url = delay_test_url
        self.max_latency_ms = max_latency_ms
        self.node_timeout_ms = node_timeout_ms
        self.switch_per_key = switch_per_key
        self.request_timeout = request_timeout
        # 状态
        self._used_nodes: set[str] = set()
        self._lock = asyncio.Lock()
        self._cached_selector: str | None = None  # 缓存自动发现的代理组名
        self._selector_type: str = ""  # 该组类型（Selector 可手动切；URLTest/Fallback 不可）
        logger.info(
            "KaringProxyController 初始化：控制端口 %s，代理出口 %s，面板 %s，集群 %s，"
            "延迟阈值 %dms，每 key 换节点=%s，secret 认证=%s",
            self.api_base, self.proxy_out, self.dashboard_url, self.cluster_url,
            self.max_latency_ms, self.switch_per_key, bool(self.secret),
        )

    # ------------------------------------------------------------------ 基础 HTTP

    async def _get(self, client: httpx.AsyncClient, path: str, **params) -> Any:
        resp = await client.get(self.api_base + path, params=params or None,
                                headers=self._auth_headers, timeout=self.request_timeout)
        if resp.status_code == 401:
            raise KaringProxyError(
                f"GET {path} -> 401 未授权：karing 控制端口已开启 secret 认证，"
                f"请在 .env 设 KARING_SECRET（见 karing service.json 的 secret 字段）")
        if resp.status_code != 200:
            raise KaringProxyError(f"GET {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    async def _put(self, client: httpx.AsyncClient, path: str, body: dict) -> None:
        resp = await client.put(self.api_base + path, json=body,
                                headers=self._auth_headers, timeout=self.request_timeout)
        if resp.status_code == 401:
            raise KaringProxyError(
                f"PUT {path} -> 401 未授权：karing 控制端口已开启 secret 认证，"
                f"请在 .env 设 KARING_SECRET（见 karing service.json 的 secret 字段）")
        if resp.status_code not in (200, 204):
            raise KaringProxyError(f"PUT {path} -> {resp.status_code}: {resp.text[:200]}")

    # ------------------------------------------------------------------ 节点列表

    async def list_proxies(self) -> dict[str, dict]:
        """GET /proxies → {name: {type, all, now, ...}, ...}"""
        async with httpx.AsyncClient() as c:
            data = await self._get(c, _EP_PROXIES)
        return data.get("proxies", {}) if isinstance(data, dict) else {}

    async def find_selector(self) -> str:
        """自动找代理组：优先 Selector（可手动 PUT 切换），无则退化为 URLTest/Fallback 组。

        sing-box 对 URLTest/Fallback 组 PUT 返回 400 "Must be a Selector"，故仅 Selector 可手动切节点；
        退化组仅用于列节点 + 测延迟做可用性验证（无法指定具体节点，沿用自动选路结果）。
        缓存 _cached_selector + _selector_type 供 pick_fast_unused_node 判断是否可 PUT。
        """
        if self._cached_selector:
            return self._cached_selector
        proxies = await self.list_proxies()
        # 第一轮：仅 Selector（可手动切换）
        sel = self._find_best_group(proxies, _SWITCHABLE_TYPES)
        if sel:
            self._cached_selector = sel
            self._selector_type = "Selector"
            logger.info("自动发现 Selector 代理组（可手动切换）：%s", sel)
            return sel
        # 第二轮兜底：任意有 all 节点的组（URLTest/Fallback，仅可列节点不可手动切）
        grp = self._find_best_group(proxies, _GROUP_TYPES)
        if grp:
            gtype = proxies[grp].get("type", "")
            self._cached_selector = grp
            self._selector_type = gtype
            logger.warning(
                "⚠️ 未找到 Selector 代理组，退化为 %s 类型组「%s」（不可手动 PUT 切换节点）。"
                "如需「每号换节点」请在 karing 配置一个 Selector 类型代理组。",
                gtype, grp)
            return grp
        raise KaringProxyError("未找到任何代理组（控制端口可能未启用 Clash API 或无可用配置）")

    def _find_best_group(self, proxies: dict, allowed_types: tuple[str, ...]) -> str | None:
        """在 proxies 中按关键词偏好选第一个指定类型且有 all 节点的组名。"""
        keywords = ("proxy", "节点", "select", "选择", "auto", "落地")
        candidates: list[tuple[int, str]] = []
        for name, info in proxies.items():
            if info.get("type") in allowed_types and info.get("all"):
                ln = name.lower()
                rank = 0
                for i, kw in enumerate(kw_lower := [k.lower() for k in keywords]):
                    if kw in ln:
                        rank = len(keywords) - i
                        break
                candidates.append((rank, name))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    async def list_nodes(self, selector: str | None = None) -> list[str]:
        """列出某组下所有节点名。sing-box 对 GLOBAL 等组 GET 单独端点可能 404，兜底用 list_proxies 的 all。"""
        selector = selector or await self.find_selector()
        # 先试单独端点 GET /proxies/{name}
        try:
            async with httpx.AsyncClient() as c:
                data = await self._get(c, _EP_PROXY.format(name=selector))
            nodes = list(data.get("all", []) or [])
            if nodes:
                return nodes
        except KaringProxyError:
            pass  # 404 等降级到 list_proxies
        # 兜底：从 list_proxies 读该组的 all
        proxies = await self.list_proxies()
        info = proxies.get(selector) or {}
        return list(info.get("all", []) or [])

    # ------------------------------------------------------------------ 延迟测试

    async def test_node_delay(self, node: str, url: str | None = None) -> int | None:
        """测单节点延迟（ms）。失败/超时返回 None。"""
        url = url or self.delay_test_url
        try:
            async with httpx.AsyncClient() as c:
                data = await self._get(
                    c, _EP_DELAY.format(name=node),
                    url=url, timeout=str(self.node_timeout_ms),
                )
            delay = data.get("delay")
            return int(delay) if delay is not None else None
        except Exception as e:
            logger.debug("节点延迟测试失败 %s: %s", node, e)
            return None

    async def test_outbound_rtt(self, url: str | None = None) -> int | None:
        """经代理出口(3067) 实测一次请求 RTT（ms），作为延迟兜底验证。"""
        url = url or self.delay_test_url
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(proxy=self.proxy_out, timeout=self.node_timeout_ms / 1000 + 2) as c:
                r = await c.get(url)
                if r.status_code in (200, 204):
                    return int((time.monotonic() - t0) * 1000)
        except Exception as e:
            logger.debug("代理出口 RTT 测试失败: %s", e)
        return None

    # ------------------------------------------------------------------ 切换

    async def switch_node(self, node: str, selector: str | None = None) -> None:
        """PUT 切换 Selector 当前节点。"""
        selector = selector or await self.find_selector()
        async with httpx.AsyncClient() as c:
            await self._put(c, _EP_PROXY.format(name=selector), {"name": node})
        logger.info("已切换 karing 出口节点 → %s（selector=%s）", node, selector)

    async def current_node(self, selector: str | None = None) -> str | None:
        """读当前 Selector 选中的节点名。"""
        selector = selector or await self.find_selector()
        async with httpx.AsyncClient() as c:
            data = await self._get(c, _EP_PROXY.format(name=selector))
        return data.get("now")

    # ------------------------------------------------------------------ 核心：每 key 换一个可用节点

    async def pick_fast_unused_node(self) -> tuple[str | None, int | None]:
        """选一个延迟 <max_latency_ms 且未用过的节点并切换。

        策略：
        1. 列出 Selector 组所有节点
        2. 优先测未用过的节点延迟，选第一个达标的
        3. 全部用完 → 重置 used 集合从头轮换
        4. 切换成功后用代理出口实测 RTT 兜底验证
        5. 任一节点都不可用 → 返回 (None, None) 不阻断（调用方退回默认）

        返回 (node_name, latency_ms)。node_name 为 None 表示无可用节点。
        """
        if not self.switch_per_key:
            return None, None
        async with self._lock:
            try:
                selector = await self.find_selector()
                nodes = await self.list_nodes(selector)
            except Exception as e:
                logger.warning("karing 列节点失败（控制端口 %s 是否启用?）: %s", self.api_base, e)
                return None, None
            if not nodes:
                logger.warning("karing Selector 组 %s 无节点", selector)
                return None, None

            # 候选：未用过的；若全用过则重置
            candidates = [n for n in nodes if n not in self._used_nodes]
            if not candidates:
                logger.info("所有节点已用过一轮，重置已用集合重新轮换（共 %d 节点）", len(nodes))
                self._used_nodes.clear()
                candidates = list(nodes)

            # 并发测延迟，选第一个达标（按候选顺序，保证轮换不重复）
            logger.info("测试 %d 个候选节点延迟（阈值 %dms）...", len(candidates), self.max_latency_ms)
            chosen: str | None = None
            chosen_latency: int | None = None
            # 逐个测：保证按顺序选第一个达标的，符合「换一个」语义
            for node in candidates:
                latency = await self.test_node_delay(node)
                if latency is not None and latency < self.max_latency_ms:
                    chosen, chosen_latency = node, latency
                    break
                logger.debug("节点 %s 延迟 %s（超过 %dms 或失败，跳过）",
                             node, latency, self.max_latency_ms)

            if chosen is None:
                # 兜底：放宽顺序，取所有候选中延迟最小且 <阈值的（并发批量测）
                logger.warning("按顺序未找到达标节点，批量重测所有候选取最小延迟...")
                results = await asyncio.gather(
                    *[self.test_node_delay(n) for n in candidates], return_exceptions=True
                )
                best: tuple[str, int] | None = None
                for node, lat in zip(candidates, results):
                    if isinstance(lat, int) and lat < self.max_latency_ms:
                        if best is None or lat < best[1]:
                            best = (node, lat)
                if best:
                    chosen, chosen_latency = best

            if chosen is None:
                logger.error("无可用节点延迟 <%dms（共 %d 候选），本次不切换，退回默认出口",
                             self.max_latency_ms, len(candidates))
                return None, None

            # 仅 Selector 类型组可 PUT 切换；URLTest/Fallback 自动选路组 PUT 必 400，跳过切换
            if self._selector_type == "Selector":
                try:
                    await self.switch_node(chosen, selector)
                except Exception as e:
                    logger.error("切换节点 %s 失败: %s", chosen, e)
                    return None, None
                self._used_nodes.add(chosen)
            else:
                # 自动选路组无法手动指定节点：沿用 karing 自动选路结果，仅记录可达节点用于日志
                logger.warning(
                    "代理组「%s」类型=%s 不可手动切换，沿用 karing 自动选路节点。"
                    "测得可达节点 %s 延迟 %dms（已验证出口可用）",
                    selector, self._selector_type, chosen, chosen_latency)

            # 出口 RTT 兜底验证
            rtt = await self.test_outbound_rtt()
            if rtt is not None and rtt > self.max_latency_ms * 2:
                logger.warning("节点 %s API 延迟 %dms 但出口实测 RTT %dms 偏高，仍采用",
                               chosen, chosen_latency, rtt)
            logger.info("✅ 本次注册出口节点=%s 延迟=%dms（组=%s 类型=%s 已用 %d/%d）",
                        chosen, chosen_latency, selector, self._selector_type,
                        len(self._used_nodes), len(nodes))
            return chosen, chosen_latency

    async def status(self) -> dict:
        """返回当前状态快照（节点/已用/selector）。"""
        async with self._lock:
            selector = self._cached_selector
            current = None
            nodes: list[str] = []
            if selector:
                try:
                    async with httpx.AsyncClient() as c:
                        data = await self._get(c, _EP_PROXY.format(name=selector))
                    current = data.get("now")
                    nodes = list(data.get("all", []) or [])
                except Exception:
                    pass
            return {
                "api_base": self.api_base,
                "proxy_out": self.proxy_out,
                "dashboard": self.dashboard_url,
                "cluster": self.cluster_url,
                "selector": selector,
                "selector_type": self._selector_type,
                "current_node": current,
                "total_nodes": len(nodes),
                "used_nodes": len(self._used_nodes),
                "max_latency_ms": self.max_latency_ms,
                "switch_per_key": self.switch_per_key,
                "secret_auth": bool(self.secret),
            }

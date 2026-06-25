"""API Key 申请与落库。

复用注册阶段保存的 storage_state（cookies）登录态，
进 Fireworks API Keys 页 → 创建 Key → 提取 Key 值 → 落库。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import AppConfig
from name_generator import gen_key_name

logger = logging.getLogger(__name__)


@dataclass
class KeyFetchResult:
    success: bool
    api_key: str | None = None
    error: str | None = None


class KeyFetcher:
    """Fireworks API Key 申请。"""

    def __init__(self, app_config: AppConfig) -> None:
        self.cfg = app_config

    async def fetch_key(self, storage_state_path: str | Path,
                        proxy: str | None = None) -> KeyFetchResult:
        """用已有登录态打开 API Keys 页，创建并提取 Key。"""
        from playwright.async_api import async_playwright

        fw = self.cfg.fireworks
        sel = fw.form_selectors
        storage_state_path = Path(storage_state_path)
        if not storage_state_path.exists():
            return KeyFetchResult(success=False, error=f"登录态文件不存在: {storage_state_path}")

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.cfg.registrar.headless,
                    proxy={"server": proxy} if proxy else None,
                )
                # 对齐 registrar 修复后策略：不写死 UA（Chrome/120 是 2023 旧版反指纹点），
                # 用 Playwright 默认 UA；stealth 默认禁用（设 false 反触发降级）。
                context = await browser.new_context(
                    storage_state=str(storage_state_path),
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                )
                if getattr(self.cfg.fireworks, "enable_stealth", False):
                    try:
                        from playwright_stealth import Stealth
                        Stealth().use_async(context)
                        logger.info("stealth 2.0 已注入 context")
                    except ImportError:
                        logger.info("playwright-stealth 未安装，跳过 stealth 注入")
                    except Exception as e:
                        logger.warning("stealth 注入失败（继续无 stealth）: %s", e)
                else:
                    logger.info("stealth 已禁用（config enable_stealth=false）")
                # 与 register 一致：删 navigator.webdriver，避免 Fireworks 返回降级 HTML
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                page = await context.new_page()

                # 1. 打开 API Keys 页
                logger.info("打开 API Keys 页 %s", fw.api_keys_url)
                await page.goto(fw.api_keys_url, wait_until="domcontentloaded", timeout=60000)

                # 2. 点 "Create API Key" 下拉按钮（菜单展开可能慢，重试点击）
                # 实测（probe_r5y.py）：Create API Key 点击后下拉有 2 个 div[role=menuitem]：
                # "API Key"（内含 <span>API Key</span> + svg）和 "Service Account API Keys"。
                # 注意：text-is('API Key') 在 div 上匹配 0（文本在 span 子节点，div 直接 textContent 不完整），
                # 必须用 :has(span:text-is('API Key')) 精确定位含该 span 的 menuitem。
                logger.info("点 Create API Key 下拉按钮")
                menu_sel = "div[role='menuitem']:has(span:text-is('API Key'))"
                menu_item = None
                for create_attempt in range(1, 4):
                    try:
                        await page.click(sel.api_key_create_button, timeout=15000)
                    except Exception as e:
                        logger.warning("点 Create API Key 失败（第 %d 次）: %s", create_attempt, e)
                    # 等下拉菜单项出现
                    try:
                        await page.wait_for_selector(menu_sel, state="visible", timeout=8000)
                        menu_item = page.locator(menu_sel).first
                        logger.info("✅ Create API Key 下拉已展开（第 %d 次）", create_attempt)
                        break
                    except Exception:
                        logger.warning("⚠️ Create API Key 下拉未展开（第 %d 次），重新点击", create_attempt)
                        if create_attempt < 3:
                            await page.wait_for_timeout(2000)
                if menu_item is None or await menu_item.count() == 0:
                    # 兜底：配置选择器 .first
                    menu_item = page.locator(sel.api_key_menu_item).first
                    if await menu_item.count() == 0:
                        try:
                            await page.screenshot(path=str(self.cfg.abs_path(self.cfg.paths.screenshots_dir) / "apikey_menu_fail.png"))
                        except Exception:
                            pass
                        raise RuntimeError("Create API Key 下拉菜单未出现 'API Key' 菜单项（重试3次）")

                # 3. 点菜单项 "API Key"
                logger.info("点菜单项 API Key（精确文字定位，排除 Service Account API Keys）")
                await menu_item.click()

                # 4. 等命名对话框，填名称
                # 诊断截图：点菜单项后看命名对话框
                try:
                    await page.screenshot(path=str(self.cfg.abs_path(self.cfg.paths.screenshots_dir) / "apikey_name_dialog.png"))
                except Exception:
                    pass
                await page.wait_for_selector(sel.api_key_name_input, state="visible", timeout=15000)
                # Key 名称随机化：避免所有 Key 都叫 "test" 的明显批量特征。
                # gen_key_name 从 5 种模式加权随机生成（如 prod-key-a3f9 / smith-42 / proj-beta-3c1d2a）。
                key_name = gen_key_name()
                logger.info("填 Key 名称（随机）: %s", key_name)
                await page.fill(sel.api_key_name_input, key_name)

                # 5. 点 Generate Key 生成
                logger.info("点 Generate Key: %s", sel.api_key_generate_button)
                await page.click(sel.api_key_generate_button)
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2000)
                # 诊断截图：Generate 后看 key 展示
                try:
                    await page.screenshot(path=str(self.cfg.abs_path(self.cfg.paths.screenshots_dir) / "apikey_generated.png"))
                    # dump 含 fw_ 的元素 + 所有 code/input/pre
                    for s in ["code", "input[readonly]", "input", "pre", "[data-testid*='key' i]", "textarea"]:
                        try:
                            els = await page.eval_on_selector_all(
                                s, "els => els.map(e => ({tag:e.tagName, val:(e.value||(e.innerText||'')).slice(0,60), visible:!!e.offsetParent}))",
                            )
                            vis = [e for e in els if e["visible"]]
                            if vis:
                                logger.info("[Generate后 %s] visible=%s", s, vis)
                        except Exception:
                            pass
                except Exception:
                    pass

                # 6. 提取 Key 值（在 <code>fw_xxx</code>，可能只展示一次）
                api_key = await self._extract_key_value(page, sel.api_key_value_selector)

                await context.close()
                await browser.close()

                if not api_key:
                    return KeyFetchResult(
                        success=False,
                        error="未提取到 API Key，请 P0 抓包确认 api_key_value_selector",
                    )
                logger.info("成功获取 API Key（前 8 位: %s...）", api_key[:8])
                return KeyFetchResult(success=True, api_key=api_key)
        except Exception as e:
            logger.exception("获取 API Key 失败")
            return KeyFetchResult(success=False, error=str(e))

    async def _extract_key_value(self, page: Any, primary_selector: str) -> str | None:
        """从页面提取 Key 值，多策略兜底。"""
        # 等一下让 Key 渲染出来
        await page.wait_for_timeout(1000)
        # 策略1：配置的主选择器
        loc = page.locator(primary_selector).first
        try:
            if await loc.count() > 0:
                tag = await loc.evaluate("el => el.tagName.toLowerCase()")
                if tag == "input":
                    return await loc.input_value()
                return (await loc.inner_text()).strip()
        except Exception as e:
            logger.debug("主选择器提取失败: %s", e)

        # 策略2：匹配 fw_ 开头的 key
        try:
            body_text = await page.inner_text("body")
            import re
            m = re.search(r"\b(fw_[A-Za-z0-9]{20,})\b", body_text)
            if m:
                return m.group(1)
        except Exception:
            pass

        # 策略3：任意 readonly input / code / pre
        for s in ["input[readonly]", "code", "pre", "[data-testid='api-key']"]:
            try:
                loc = page.locator(s).first
                if await loc.count() > 0:
                    tag = await loc.evaluate("el => el.tagName.toLowerCase()")
                    val = (await loc.input_value()) if tag == "input" else (await loc.inner_text()).strip()
                    if val and len(val) > 15:
                        return val
            except Exception:
                continue
        return None


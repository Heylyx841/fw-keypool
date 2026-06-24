"""Fireworks 注册流程自动化（Playwright + stealth 2.0）。

完整流程（已抓包固化 2026-06-23）：
注册(2步) → 验证邮件(链接) → 登录 → onboarding(2页) → 保存登录态

选择器/端点全部从 config.yaml 读取。反爬：CaptchaSolver 可插拔。
"""
from __future__ import annotations

import logging
import random
import secrets
import string
from dataclasses import dataclass
from typing import Any

from config import AppConfig
from email_pool import EmailAccount
from mail_fetcher import FakeEmailError, MailFetcher, make_fetcher
from name_generator import gen_full_name

logger = logging.getLogger(__name__)


def _random_nonempty_subset(options: list[str]) -> list[str]:
    """从选项列表中随机选取一个非空子集（不定项，至少选 1 项）。

    用于 onboarding 问卷：每组问题随机勾选 >=1 个选项，降低固定选项指纹风险。
    返回保持原顺序的选中项（便于日志可读）。
    """
    if not options:
        return []
    # 随机选取 1..len(options) 个，再从中按原顺序取出
    k = random.randint(1, len(options))
    chosen = set(random.sample(range(len(options)), k))
    return [options[i] for i in range(len(options)) if i in chosen]


def _gen_password(length: int = 16) -> str:
    """生成强随机密码，满足 Fireworks 密码策略：
    - 至少 8 字符（取 16 更稳）
    - 含大写字母、小写字母、数字、特殊字符
    """
    length = max(length, 12)
    specials = "!@#$%^*"
    alphabet = string.ascii_letters + string.digits + specials
    pwd = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(specials),
    ]
    pwd += [secrets.choice(alphabet) for _ in range(length - len(pwd))]
    sec_list = list(pwd)
    secrets.SystemRandom().shuffle(sec_list)
    return "".join(sec_list)


@dataclass
class CaptchaSolver:
    """反爬验证可插拔接口。默认 NoneSolver 直接放行。

    实现期若发现验证码，新增 ManualSolver / TwoCaptchaSolver 子类，
    在 config.yaml captcha_strategy 切换即可，主流程不动。
    """

    strategy: str = "none"

    def solve(self, page: Any, selector_hint: str | None = None) -> bool:
        return True


def make_captcha_solver(app_config: AppConfig) -> CaptchaSolver:
    strategy = app_config.fireworks.captcha_strategy.lower()
    if strategy == "none":
        return CaptchaSolver(strategy="none")
    if strategy == "manual":
        return _ManualSolver()
    if strategy == "2captcha":
        return _TwoCaptchaSolver(app_config.two_captcha_api_key)
    logger.warning("未知 captcha_strategy %s，回退 none", strategy)
    return CaptchaSolver(strategy="none")


class _ManualSolver(CaptchaSolver):
    def __init__(self) -> None:
        super().__init__(strategy="manual")

    def solve(self, page: Any, selector_hint: str | None = None) -> bool:
        input("⚠️ 检测到验证码，请在浏览器手动完成后回终端按 Enter 继续...")
        return True


class _TwoCaptchaSolver(CaptchaSolver):
    def __init__(self, api_key: str) -> None:
        super().__init__(strategy="2captcha")
        self.api_key = api_key
        if not api_key:
            logger.warning("2captcha API key 未配置，打码将失败")

    def solve(self, page: Any, selector_hint: str | None = None) -> bool:
        raise NotImplementedError("2captcha 打码实现待实现期按验证码类型补全")


@dataclass
class RegistrationResult:
    success: bool
    email: str
    password: str
    fireworks_user_id: str | None = None
    error: str | None = None
    storage_state: str | None = None
    is_fake_email: bool = False  # 假邮箱判定（POP 登录连续失败超阈值），orchestrator 应禁用邮箱不再重试


class FireworksRegistrar:
    """Fireworks 注册流程。"""

    def __init__(self, app_config: AppConfig) -> None:
        self.cfg = app_config
        self.captcha = make_captcha_solver(app_config)

    async def register(self, account: EmailAccount, password: str | None = None,
                       proxy: str | None = None, storage_state_path: str | None = None,
                       on_pop_login_fail=None) -> RegistrationResult:
        """执行单邮箱注册全流程。"""
        from playwright.async_api import async_playwright

        password = password or _gen_password()
        fw = self.cfg.fireworks
        sel = fw.form_selectors

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.cfg.registrar.headless,
                    proxy={"server": proxy} if proxy else None,
                )
                # 不写死 user_agent：Chrome/120 是 2023 旧版本，写死反而成为反指纹点。
                # 用 Playwright 默认 UA（跟随 Chromium 内核版本，headed 下是真实最新 Chrome UA）。
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                )
                # stealth 2.0：默认禁用（use_async 与 Fireworks 页面冲突可能导致页面崩溃）
                # 需要时在 config.yaml 设 enable_stealth: true 开启
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

                # 关键：手动删除 navigator.webdriver 标志。
                # 实测（probe_step1.py）：navigator.webdriver=true → Fireworks 返回降级纯 HTML 页（无 React）；
                # add_init_script 返回 undefined → 返回正常 React SPA（form 有 onSubmit，input 有 _valueTracker）。
                # stealth 2.0 把 webdriver 设为 false 反而触发降级，故禁用 stealth，仅用 init_script。
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                logger.info("已 add_init_script 隐藏 navigator.webdriver")

                page = await context.new_page()

                # ===== 注册 第1步：邮箱页（含降级页检测 + 自动重试）=====
                # 实测（probe_step1.py + run.py 实跑）：连续多次访问 signup 会触发 Fireworks 频率限制，
                # 间歇性返回降级纯 HTML 页（无 React，input 无 _valueTracker，form method=get 走原生 GET）。
                # 此时 form.requestSubmit() 会走原生 GET（URL 变 signup?email=...）而非 React onSubmit 跳转。
                # 解决：加载后检测降级页（_valueTracker 缺失），若降级则等待 + 重新加载重试。
                max_page_retry = 3
                for attempt in range(1, max_page_retry + 1):
                    logger.info("打开注册页 %s（邮箱 %s，第 %d/%d 次尝试）", fw.signup_url, account.email, attempt, max_page_retry)
                    # domcontentloaded + hydrate 等待：load 事件在 Cloudflare 后面页面可能永不触发，
                    # domcontentloaded 太早（React 未 hydrate），故加 2s 等待补足。
                    await page.goto(fw.signup_url, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(2000)  # 等 React hydrate

                    # 降级页检测：React 受控 input 有 _valueTracker；降级 HTML 页无。
                    if await self._is_degraded_page(page, sel.email_input):
                        logger.warning("⚠️ 检测到降级 HTML 页（无 React _valueTracker），第 %d 次。等待 8s 后重新加载...", attempt)
                        await page.wait_for_timeout(8000)  # 等频率限制消退
                        if attempt < max_page_retry:
                            continue  # 重新 goto
                        logger.error("❌ 降级页重试 %d 次仍失败，放弃", max_page_retry)
                        raise RuntimeError(f"第1步降级页：连续 {max_page_retry} 次加载均返回降级 HTML（Fireworks 频率限制），请稍后重试")
                    logger.info("✅ 页面正常（React SPA，第 %d 次加载成功）", attempt)
                    break  # 正常页，跳出重试循环

                logger.info("第1步：填邮箱 %s", account.email)
                # React 受控 input：page.fill 不可靠（_valueTracker 不重置则 state 不更新），统一用 _fill_react_input
                await self._fill_react_input(page, sel.email_input, account.email)
                # 截图调试：填邮箱后的状态
                try:
                    shot0 = self.cfg.abs_path(self.cfg.paths.screenshots_dir) / f"after_fill_email_{account.email}.png"
                    await page.screenshot(path=str(shot0))
                except Exception:
                    pass

                if not self.captcha.solve(page):
                    raise RuntimeError("第1步验证码未通过")

                # 提交第1步：用 form.requestSubmit() 触发 form 的 submit 事件 → 走 React onSubmit handler。
                # 实测（probe_step1b.py）：keyboard.press Enter / click submit 都无法触发 React onSubmit 跳转，
                # 唯有 form.requestSubmit() 成功跳第二页（密码框出现）。click submit 还会触发原生 GET ?email=。
                logger.info("第1步提交：form.requestSubmit()（触发 React onSubmit）")
                await self._submit_form(page)
                await page.wait_for_timeout(2000)
                logger.info("第1步已提交，等待第2步密码页...")
                # 截图调试：看点 Next 后页面状态
                try:
                    shot = self.cfg.abs_path(self.cfg.paths.screenshots_dir) / f"after_next_{account.email}.png"
                    await page.screenshot(path=str(shot))
                    logger.info("已截图: %s", shot.name)
                except Exception:
                    pass
                # 探测：点 Next 后的 URL + 所有 input 状态
                try:
                    cur_url = page.url
                    inputs = await page.eval_on_selector_all(
                        "input",
                        "els => els.map(e => ({id:e.id, name:e.name, type:e.type, visible:!!e.offsetParent}))",
                    )
                    logger.info("点 Next 后 URL=%s", cur_url)
                    logger.info("点 Next 后 inputs=%s", inputs)
                except Exception as e:
                    logger.warning("探测 DOM 失败: %s", e)

                # 提交后降级页检测：若 URL 走原生 GET（含 ?email=）且无密码框，说明提交时页面已降级。
                if await self._is_native_get_after_submit(page, account.email):
                    logger.warning("⚠️ 第1步提交后检测到原生 GET（URL 含 ?email=，无 React onSubmit 跳转），页面可能降级")
                    raise RuntimeError("第1步提交后走原生 GET（降级页），请重试")

                # 等第2步密码框（加大到 60s，Fireworks/Cloudflare 可能慢）
                await page.wait_for_selector(sel.password_input, state="visible", timeout=60000)

                # ===== 注册 第2步：密码页 =====
                # 密码框同样是 React 受控 input（input#password / input#confirm-password）。
                # 实测根因（登录 Invalid）：之前用 page.fill 填密码，React state 未更新 →
                # Fireworks 端实际记录的密码 ≠ 本地 password 变量 → 登录时用同一 password 仍 Invalid。
                # 修复：统一用 _fill_react_input（nativeInputValueSetter + 重置 _valueTracker + InputEvent）。
                logger.info("第2步：填密码（长度 %d）", len(password))
                await self._fill_react_input(page, sel.password_input, password)
                if getattr(sel, "confirm_password_input", ""):
                    await self._fill_react_input(page, sel.confirm_password_input, password)

                if not self.captcha.solve(page):
                    raise RuntimeError("第2步验证码未通过")

                # 第2步提交：同样用 form.requestSubmit() 触发 React onSubmit（与第1步一致，实测可靠）。
                # click Create Account 在 webdriver 隐藏后可能仍走原生 GET，统一用 requestSubmit。
                logger.info("第2步提交：form.requestSubmit()")
                await self._submit_form(page)
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
                logger.info("第2步已提交，等待验证邮件 %s ...", account.email)

                # 轮询验证邮件
                fetcher = make_fetcher(account, self.cfg, on_pop_login_fail=on_pop_login_fail)
                vc = fetcher.fetch_verification(
                    sender_pattern=fw.email_sender_pattern,
                    subject_pattern=fw.email_subject_pattern,
                )

                # 完成验证（验证链接型：直接 goto 验证 URL）
                if vc.kind == "code":
                    code_input = await self._find_first(
                        page,
                        [
                            "input[name='code']", "input[name='otp']",
                            "input[name='verification']", "input[name='verificationCode']",
                            "input[placeholder*='code' i]", "input[placeholder*='verification' i]",
                        ],
                    )
                    if not code_input:
                        raise RuntimeError("未找到验证码输入框")
                    await page.fill(code_input, vc.value)
                    submit2 = await self._find_first(
                        page, ["button[type='submit']", "button:has-text('Verify')", "button:has-text('Submit')"]
                    )
                    if submit2:
                        await page.click(submit2)
                else:  # link
                    logger.info("点验证链接: %s", vc.value[:80])
                    await page.goto(vc.value, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
                # 诊断：验证完成后页面 URL + 是否已登录态（验证链接可能自动登录）
                await page.wait_for_timeout(3000)
                logger.info("验证完成后 URL=%s", page.url)
                try:
                    body_snippet = (await page.evaluate("document.body.innerText") or "")[:200]
                    logger.info("验证完成后页面文本: %s", body_snippet.replace("\n", " "))
                except Exception:
                    pass
                logger.info("邮箱验证完成 %s", account.email)

                # ===== 登录 =====
                # 注意：验证链接完成后可能已是登录态，goto login_url 会被重定向到主页/onboarding。
                # 先检测当前是否已登录（login 表单是否出现），若已登录则跳过手动登录。
                logger.info("开始登录 %s ...", account.email)
                await page.goto(fw.login_url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2000)  # 等 React hydrate + 可能的重定向
                logger.info("goto login 后 URL=%s", page.url)
                login_email_visible = await page.locator(sel.login_email_input).count() > 0
                if login_email_visible:
                    # login 表单出现，需手动登录。
                    # 登录页 email/password 同为 React 受控 input（data-testid=login-form-*），
                    # 用 page.fill 同样不可靠，统一用 _fill_react_input，确保 React state 更新。
                    logger.info("login 表单可见，手动登录")
                    await self._fill_react_input(page, sel.login_email_input, account.email)
                    await self._fill_react_input(page, sel.login_password_input, password)
                    if not self.captcha.solve(page):
                        raise RuntimeError("登录验证码未通过")
                    await page.click(sel.login_submit_button)
                    await page.wait_for_load_state("domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(3000)
                    logger.info("登录已提交，URL=%s，等待 onboarding...", page.url)
                else:
                    # 验证后已自动登录，login 页被重定向
                    logger.info("login 表单不可见（验证后已自动登录或被重定向），URL=%s", page.url)

                # 诊断：登录后页面文本（判断是否 onboarding / 主页 / 其他）
                try:
                    body_snippet = (await page.evaluate("document.body.innerText") or "")[:300]
                    logger.info("登录后页面文本: %s", body_snippet.replace("\n", " "))
                except Exception:
                    pass

                # ===== onboarding 问卷 =====
                # 账号可能已完成 onboarding（已注册过的账号登录后直接进主页），firstName 不会出现。
                # 检测：若 firstName 短时间内不出现，判定 onboarding 已完成，跳过直接进保存登录态。
                try:
                    await page.wait_for_selector(sel.onboarding_firstname_input, state="visible", timeout=15000)
                except Exception:
                    logger.info("onboarding firstName 未出现（账号可能已完成 onboarding），跳过问卷直接保存登录态")
                    # 保存登录态
                    storage_state = storage_state_path
                    if storage_state:
                        await context.storage_state(path=storage_state)
                    fireworks_user_id = await self._try_get_user_id(page)
                    await context.close()
                    await browser.close()
                    return RegistrationResult(
                        success=True,
                        email=account.email,
                        password=password,
                        fireworks_user_id=fireworks_user_id,
                        storage_state=storage_state,
                    )
                await self._do_onboarding(page, account, sel)
                logger.info("onboarding 完成 %s", account.email)

                # 保存登录态
                storage_state = storage_state_path
                if storage_state:
                    await context.storage_state(path=storage_state)

                fireworks_user_id = await self._try_get_user_id(page)

                await context.close()
                await browser.close()

                return RegistrationResult(
                    success=True,
                    email=account.email,
                    password=password,
                    fireworks_user_id=fireworks_user_id,
                    storage_state=storage_state,
                )
        except FakeEmailError as e:
            logger.error("🚫 假邮箱判定 %s: %s", account.email, e)
            return RegistrationResult(success=False, email=account.email, password=password,
                                      error=str(e), is_fake_email=True)
        except Exception as e:
            logger.exception("注册失败 %s", account.email)
            return RegistrationResult(success=False, email=account.email, password=password, error=str(e))

    async def login_and_onboard(self, account: EmailAccount, password: str,
                                proxy: str | None = None,
                                storage_state_path: str | None = None) -> RegistrationResult:
        """从登录开始的入口（跳过注册+验证，方便单独测试登录→onboarding→保存登录态）。

        用途：账号已在 Fireworks 注册并验证，但 onboarding/登录态需重新走一遍时调用。
        典型：login_test.py 命令行指定邮箱 + 从 DB 读密码，单独测登录链路。
        """
        from playwright.async_api import async_playwright

        fw = self.cfg.fireworks
        sel = fw.form_selectors
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.cfg.registrar.headless,
                    proxy={"server": proxy} if proxy else None,
                )
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                )
                # 与 register 一致：仅 add_init_script 删 webdriver，不写死 UA，不用 stealth
                if getattr(fw, "enable_stealth", False):
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
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                page = await context.new_page()

                # ===== 登录 =====
                logger.info("[login_test] 登录 %s ...", account.email)
                await page.goto(fw.login_url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2000)
                logger.info("[login_test] goto login 后 URL=%s", page.url)
                login_email_visible = await page.locator(sel.login_email_input).count() > 0
                if login_email_visible:
                    # 降级页检测
                    if await self._is_degraded_page(page, sel.login_email_input):
                        raise RuntimeError("登录页降级（无 React），请稍后重试")
                    logger.info("[login_test] login 表单可见，手动登录")
                    await self._fill_react_input(page, sel.login_email_input, account.email)
                    await self._fill_react_input(page, sel.login_password_input, password)
                    if not self.captcha.solve(page):
                        raise RuntimeError("登录验证码未通过")
                    await page.click(sel.login_submit_button)
                    await page.wait_for_load_state("domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(3000)
                    logger.info("[login_test] 登录已提交，URL=%s", page.url)
                else:
                    logger.info("[login_test] login 表单不可见（已登录或被重定向），URL=%s", page.url)

                # ===== onboarding =====
                try:
                    await page.wait_for_selector(sel.onboarding_firstname_input, state="visible", timeout=15000)
                except Exception:
                    logger.info("[login_test] onboarding firstName 未出现（账号已完成 onboarding），直接保存登录态")
                    storage_state = storage_state_path
                    if storage_state:
                        await context.storage_state(path=storage_state)
                    fireworks_user_id = await self._try_get_user_id(page)
                    await context.close()
                    await browser.close()
                    return RegistrationResult(
                        success=True, email=account.email, password=password,
                        fireworks_user_id=fireworks_user_id, storage_state=storage_state,
                    )
                await self._do_onboarding(page, account, sel)
                logger.info("[login_test] onboarding 完成 %s", account.email)

                # 保存登录态
                storage_state = storage_state_path
                if storage_state:
                    await context.storage_state(path=storage_state)
                fireworks_user_id = await self._try_get_user_id(page)
                await context.close()
                await browser.close()
                return RegistrationResult(
                    success=True, email=account.email, password=password,
                    fireworks_user_id=fireworks_user_id, storage_state=storage_state,
                )
        except Exception as e:
            logger.exception("[login_test] 登录测试失败 %s", account.email)
            return RegistrationResult(success=False, email=account.email, password=password, error=str(e))

    async def _do_onboarding(self, page: Any, account: EmailAccount, sel: Any) -> None:
        """完成 onboarding 问卷（两页式）。

        第1页 ProfileSection：填姓名 + 勾 Terms of Service + Continue
        第2页 QuestionnaireSection：勾 2 个问卷 checkbox + Submit to get $6 Credits

        防卡死策略（用户思路：检测没进下一步就刷新）：
        - 第1页降级页检测（_is_degraded_page 复用）：降级则刷新重试。
        - Continue 按钮 disabled 检测：disabled 说明 React state 未同步/Terms 未勾 → 重新填值+勾选。
        - Continue 点击后检测第2页 done 按钮是否出现：未出现则刷新整个 onboarding 重试（最多 3 次）。
          刷新能消除降级页 + React state 不同步 + 间歇性卡死。
        """
        max_retry = 3
        for attempt in range(1, max_retry + 1):
            # 每次重试都重新生成随机姓名（刷新重试时避免用同一个被风控标记的名字）
            first_name, last_name = gen_full_name()
            logger.info("onboarding 第1页（第 %d/%d 次尝试）：填 firstName/lastName = %s %s",
                        attempt, max_retry, first_name, last_name)
            # 等 firstName 出现
            await page.wait_for_selector(sel.onboarding_firstname_input, state="visible", timeout=30000)

            # 降级页检测：onboarding 也可能间歇性返回降级 HTML（无 React）。
            if await self._is_degraded_page(page, sel.onboarding_firstname_input):
                logger.warning("⚠️ onboarding 第1页检测到降级 HTML（无 React），第 %d 次。等待 8s 后刷新重试...", attempt)
                await page.wait_for_timeout(8000)
                if attempt < max_retry:
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(2000)
                    continue
                raise RuntimeError(f"onboarding 第1页降级：连续 {max_retry} 次均返回降级 HTML，请稍后重试")

            # 填姓名（React 受控 input，必须用 _fill_react_input 更新 state）
            await self._fill_react_input(page, sel.onboarding_firstname_input, first_name)
            await self._fill_react_input(page, sel.onboarding_lastname_input, last_name)
            logger.info("onboarding 第1页：勾选 Terms of Service")
            await self._click_checkbox_by_index(page, sel.onboarding_terms_checkbox, 0)
            await page.wait_for_timeout(500)  # 等 React state 同步后 Continue enable

            # 点 Continue（多选择器兜底）
            step1_btn = await self._find_first(
                page,
                [
                    sel.onboarding_step1_next,
                    "button:has-text('Continue')",
                    "button[type='submit']:has-text('Next')",
                    "button[type='submit']",
                ],
            )
            if not step1_btn:
                raise RuntimeError("onboarding 第1页未找到 Continue 按钮")

            # Continue disabled 检测：disabled 说明 Terms 未勾上或 React state 未同步。
            # Playwright click disabled 按钮不报错但不触发 onClick → 卡住（问题1根因）。
            is_disabled = False
            try:
                is_disabled = await page.locator(step1_btn).first.is_disabled()
                logger.info("Continue 按钮 disabled=%s，选择器=%s", is_disabled, step1_btn)
            except Exception as e:
                logger.warning("检测 Continue disabled 失败: %s", e)
            if is_disabled:
                logger.warning("⚠️ Continue 按钮 disabled（Terms 未勾或 state 未同步），重新勾选 Terms 并等待 2s...", )
                await self._click_checkbox_by_index(page, sel.onboarding_terms_checkbox, 0)
                await page.wait_for_timeout(2000)
                try:
                    is_disabled = await page.locator(step1_btn).first.is_disabled()
                except Exception:
                    is_disabled = False
                if is_disabled and attempt < max_retry:
                    logger.warning("Continue 仍 disabled，刷新 onboarding 重试")
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(2000)
                    continue
                if is_disabled:
                    raise RuntimeError("onboarding 第1页 Continue 按钮持续 disabled，无法进入下一步")

            await page.click(step1_btn)
            await page.wait_for_timeout(3000)

            # 检测是否成功进入第2页：done 按钮出现则成功，否则刷新重试（用户思路）。
            reached_step2 = False
            try:
                await page.wait_for_selector(sel.onboarding_done_button, state="visible", timeout=15000)
                reached_step2 = True
            except Exception:
                logger.warning("⚠️ Continue 后第2页未出现（第 %d 次），可能降级/state 卡死。刷新 onboarding 重试...", attempt)
            if reached_step2:
                logger.info("✅ onboarding 已进入第2页")
                break
            if attempt < max_retry:
                # 刷新重试整个第1页
                await page.goto(self.cfg.fireworks.onboarding_url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2000)
                continue
            raise RuntimeError(f"onboarding 第1页连续 {max_retry} 次点击 Continue 均未进入第2页，请检查截图")

        # ===== 第2页 =====
        # 问卷有两组问题，每组随机不定项选择（至少选 1 项），降低固定选项指纹风险。
        # 选项通过 label 文字定位 checkbox（_click_checkbox_by_option_text），比动态 id 稳定。
        group1_options = list(getattr(sel, "onboarding_questionnaire_group1_options", []) or [])
        group2_options = list(getattr(sel, "onboarding_questionnaire_group2_options", []) or [])
        # 兜底：若配置未提供新分组字段，回退到旧的两组单选项（保持向后兼容）
        if not group1_options:
            group1_options = [getattr(sel, "onboarding_questionnaire_option_1_text", "Prototype with open models")]
        if not group2_options:
            group2_options = [getattr(sel, "onboarding_questionnaire_option_2_text", "Code Assistance")]

        logger.info("onboarding 第2页：随机不定项勾选问卷选项（按文字定位）")
        for group_idx, options in enumerate((group1_options, group2_options), start=1):
            chosen = _random_nonempty_subset(options)
            logger.info("onboarding 第2页 第%d组：从 %d 个选项中随机勾选 %d 项 → %s",
                        group_idx, len(options), len(chosen), chosen)
            for opt_text in chosen:
                await self._click_checkbox_by_option_text(
                    page, sel.onboarding_questionnaire_checkbox, opt_text
                )
            await page.wait_for_timeout(300)  # 等 React state 同步
        # Submit to get $6 Credits：点击后等待页面跳转（onboarding 完成 → 进主页/账户页）。
        # 实测：该提交耗时较长，且可能需要重复点击多次才触发跳转。
        # 策略：循环点击 done 按钮，每次点击后等跳转（URL 离开 onboarding 或 done 按钮消失），最多 5 次。
        submitted = False
        for submit_attempt in range(1, 6):
            logger.info("onboarding 第2页：点 Submit to get（第 %d 次）", submit_attempt)
            try:
                await page.click(sel.onboarding_done_button, timeout=10000)
            except Exception as e:
                logger.warning("点 Submit 失败（第 %d 次）: %s", submit_attempt, e)
            # 等跳转：URL 变化 或 done 按钮消失（说明已提交成功跳走）
            try:
                await page.wait_for_url(lambda u: "onboarding" not in u, timeout=20000)
                submitted = True
                logger.info("✅ Submit 后已跳转离开 onboarding，URL=%s", page.url)
                break
            except Exception:
                # URL 没变，检查 done 按钮是否还在
                still_here = await page.locator(sel.onboarding_done_button).count() > 0
                if not still_here:
                    submitted = True
                    logger.info("✅ Submit 后 done 按钮消失（已跳转），URL=%s", page.url)
                    break
                logger.warning("⚠️ Submit 第 %d 次后仍未跳转，done 按钮仍在，等待 3s 后重试...", submit_attempt)
                await page.wait_for_timeout(3000)
        if not submitted:
            # 兜底：最后等一次 domcontentloaded
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass
            logger.warning("onboarding 第2页 Submit 重试 5 次未确认跳转，继续流程（URL=%s）", page.url)
        else:
            await page.wait_for_load_state("domcontentloaded", timeout=60000)

    async def _click_checkbox_by_index(self, page: Any, selector: str, index: int) -> None:
        """点击第 index 个匹配的 checkbox（用 nth）。"""
        loc = page.locator(selector).nth(index)
        if await loc.count() > 0:
            checked = await loc.get_attribute("aria-checked")
            if checked != "true":
                await loc.click()
                logger.debug("勾选 checkbox #%d", index)

    async def _click_checkbox_by_option_text(self, page: Any, checkbox_selector: str, option_text: str) -> None:
        """按选项文字定位问卷 checkbox。

        实际结构：<div><button role=checkbox/><label>文字</label></div>
        xpath：找文字匹配的 label，定位其祖先容器里的 button[role=checkbox]
        """
        checkbox_xpath = (
            f"//label[normalize-space(.)='{option_text}']/"
            f"ancestor::div[button[@role='checkbox']][1]/button[@role='checkbox']"
        )
        loc = page.locator(f"xpath={checkbox_xpath}").first
        if await loc.count() > 0:
            checked = await loc.get_attribute("aria-checked")
            if checked != "true":
                await loc.click()
                logger.info("勾选问卷选项: %s", option_text)
            else:
                logger.debug("问卷选项已勾选: %s", option_text)
        else:
            # 兜底1：点 label
            text_loc = page.locator(f"label:has-text('{option_text}')").first
            if await text_loc.count() > 0:
                await text_loc.click()
                logger.info("点击问卷 label: %s", option_text)
            else:
                # 兜底2：任意含文字元素
                any_loc = page.locator(f"text='{option_text}'").first
                if await any_loc.count() > 0:
                    await any_loc.click()
                    logger.info("点击问卷文字: %s", option_text)
                else:
                    logger.warning("未找到问卷选项: %s", option_text)

    async def _fill_react_input(self, page: Any, selector: str, value: str) -> None:
        """填 React 受控 input：用 nativeInputValueSetter + InputEvent 确保 React state 更新。

        React 受控组件用 _valueTracker 跟踪值。仅设 DOM value 不够，需重置 tracker
        再派发 InputEvent，React 才会认为值变了并更新 state。
        """
        loc = page.locator(selector).first
        await loc.click()              # 聚焦
        # React 受控组件填值标准方法：重置 _valueTracker + nativeSetter + InputEvent
        await loc.evaluate(
            """(el, val) => {
                const nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                nativeSetter.call(el, val);
                // 重置 React 的 _valueTracker，让 React 认为值变了
                if (el._valueTracker) { el._valueTracker.setValue(''); }
                el.dispatchEvent(new InputEvent('input', { bubbles: true, data: val }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )
        await page.wait_for_timeout(500)
        val = await loc.input_value()
        logger.info("React input 填入后 DOM value=%r", val)
        # 探测：检查 React 是否真的更新了 state（通过 _valueTracker）
        try:
            tracker = await loc.evaluate(
                "el => ({trackerVal: el._valueTracker ? el._valueTracker.value : 'no_tracker', domVal: el.value})"
            )
            logger.info("React state 探测: %s", tracker)
        except Exception:
            pass
        if val != value:
            # 兜底：type 逐字符
            logger.warning("evaluate 后值仍不对，改用 type 兜底")
            await loc.click()
            await loc.fill("")
            await loc.type(value, delay=50)
            val2 = await loc.input_value()
            logger.info("type 兜底后值=%r", val2)
            if val2 != value:
                raise RuntimeError(f"输入失败：evaluate 和 type 都没填入期望值（实际 {val2!r}）")

    async def _fill(self, page: Any, selector: str, value: str) -> None:
        try:
            await page.fill(selector, value)
        except Exception as e:
            logger.warning("填充 %s 失败: %s", selector, e)
            raise

    async def _find_first(self, page: Any, selectors: list[str]) -> str | None:
        for s in selectors:
            if await page.locator(s).count() > 0:
                return s
        return None

    async def _is_degraded_page(self, page: Any, email_selector: str) -> bool:
        """检测当前页面是否为降级纯 HTML 页（无 React）。

        实测（probe_step1.py）：Fireworks 频率限制时返回降级 HTML 页，
        其 input 没有 React 的 _valueTracker（正常 React SPA 的受控 input 有）。
        检测策略：email input 是否有 _valueTracker + form 是否有 onSubmit（__reactProps$）。
        """
        try:
            loc = page.locator(email_selector).first
            if await loc.count() == 0:
                # 连 email input 都没有，可能是完全不同的降级页
                logger.warning("降级页检测：未找到 email input %s", email_selector)
                return True
            has_tracker = await loc.evaluate(
                "el => !!(el._valueTracker || (el.parentNode && el.parentNode.__reactProps$))"
            )
            # form 是否有 React onSubmit（降级 HTML 页 form 无 __reactProps$）
            has_react_form = await page.evaluate(
                """() => {
                    const f = document.querySelector('form');
                    if (!f) return false;
                    // React 17+ 把 props 挂在 __reactProps$ 上
                    const keys = Object.keys(f);
                    return keys.some(k => k.startsWith('__reactProps'));
                }"""
            )
            degraded = not has_tracker and not has_react_form
            if degraded:
                logger.info("降级页检测：has_tracker=%s has_react_form=%s → 降级", has_tracker, has_react_form)
            return degraded
        except Exception as e:
            logger.warning("降级页检测异常（保守判定非降级）: %s", e)
            return False

    async def _is_native_get_after_submit(self, page: Any, email: str) -> bool:
        """检测第1步提交后是否走了原生 GET（降级页特征）。

        正常 React onSubmit 提交后 URL 不变或跳第2步密码页；
        降级 HTML 页 form method=get 会把 email 作为 query param：signup?email=xxx。
        """
        try:
            cur_url = page.url
            # URL 含 ?email= 且 email 出现在 query 中 → 原生 GET
            if "?" in cur_url and email.split("@")[0] in cur_url:
                # 进一步确认：密码框是否出现（正常应跳第2步）
                has_pwd = await page.locator(self.cfg.fireworks.form_selectors.password_input).count() > 0
                if not has_pwd:
                    return True
            return False
        except Exception:
            return False

    async def _submit_form(self, page: Any) -> None:
        """提交当前页面 form：用 form.requestSubmit() 触发 submit 事件。

        实测（probe_step1b.py）：React 受控表单的 onSubmit handler 只能通过 form 的 submit
        事件触发；keyboard.press Enter / click submit 都无法可靠触发 React onSubmit 跳转，
        click submit 在某些情况下还会触发原生 GET 导航。form.requestSubmit() 直接派发
        submit 事件，走 React onSubmit preventDefault + setState 路径，可靠跳转。

        兜底：若无 form 元素，回退到 click submit button（适用于非表单按钮场景）。
        """
        has_form = await page.evaluate("() => !!document.querySelector('form')")
        if has_form:
            await page.evaluate(
                "() => { const f = document.querySelector('form'); if (f) f.requestSubmit(); }"
            )
            logger.info("form.requestSubmit() 已调用")
        else:
            sel = self.cfg.fireworks.form_selectors
            submit = getattr(sel, "password_submit_button", "") or sel.submit_button
            logger.warning("未找到 form，回退 click submit button: %s", submit)
            await page.click(submit)

    async def _try_get_user_id(self, page: Any) -> str | None:
        """P0 实现期：从页面 URL 或用户接口提取 user id。"""
        return None

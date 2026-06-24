"""配置加载：读取 config.yaml + .env 环境变量，合并为运行期配置。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class FormSelectors:
    # ===== 注册 第1步：邮箱页 =====
    email_input: str = "input#email-display"
    name_input: str = ""  # 注册无姓名字段
    submit_button: str = "button[type='submit']:text-is('Next')"  # 精确匹配，避免 'Next slide'
    # ===== 注册 第2步：密码页 =====
    password_input: str = "input#password"
    confirm_password_input: str = "input#confirm-password"
    password_submit_button: str = "button[type='submit']:has-text('Create Account')"
    # ===== 登录页 =====
    login_email_input: str = "input[data-testid='login-form-email']"
    login_password_input: str = "input[data-testid='login-form-password']"
    login_submit_button: str = "button[data-testid='login-form-submit']"
    # ===== onboarding 问卷页（两页式）=====
    # 第1页 ProfileSection
    onboarding_firstname_input: str = "input[name='firstName']"
    onboarding_lastname_input: str = "input[name='lastName']"
    onboarding_terms_checkbox: str = "button[role='checkbox']"      # 第1页 Terms
    onboarding_step1_next: str = "button:has-text('Continue')"      # 第1页 Continue
    # 第2页 QuestionnaireSection（按选项文字定位 checkbox，比动态 id 稳定）
    # 两组问卷问题，每组随机不定项选择（至少选 1 项，降低固定选项指纹风险）
    # 第1组：8 个选项（"你为什么使用 Fireworks"类问题）
    onboarding_questionnaire_group1_options: list[str] = field(default_factory=lambda: [
        "Prototype with open models",
        "Flexible capacity for experimentation",
        "Flexible capacity for production",
        "Faster speeds or lower costs",
        "Fine-tune models for quality",
        "High reliability inference for production",
        "Migrate from closed to open models",
        "Migrate from self-hosting to third-party",
    ])
    # 第2组：5 个选项（"你的使用场景"类问题）
    onboarding_questionnaire_group2_options: list[str] = field(default_factory=lambda: [
        "Code Assistance",
        "Conversational AI",
        "Agentic AI",
        "Search",
        "Multimedia RAG",
    ])
    onboarding_questionnaire_checkbox: str = "button[role='checkbox']"
    onboarding_done_button: str = "button[name='done']:has-text('Submit to get')"
    # 兼容旧字段（保留以避免 config.yaml 读取报错，但不再用于实际勾选）
    onboarding_questionnaire_option_1_text: str = "Prototype with open models"
    onboarding_questionnaire_option_2_text: str = "Code Assistance"
    # ===== API Key 页 =====
    api_key_create_button: str = "button:has-text('Create API Key')"
    api_key_menu_item: str = "div[role='menuitem']:has-text('API Key')"
    api_key_name_input: str = "input#name"
    api_key_generate_button: str = "button[type='submit']:has-text('Generate Key')"
    api_key_value_selector: str = "code"


@dataclass
class FireworksConfig:
    signup_url: str = "https://app.fireworks.ai/signup"
    login_url: str = "https://app.fireworks.ai/login/email?redirectURI=%2Faccount%2Fhome"
    onboarding_url: str = "https://app.fireworks.ai/onboarding"
    api_keys_url: str = "https://app.fireworks.ai/settings/users/api-keys"
    base_url: str = "https://api.fireworks.ai/inference/v1"
    form_selectors: FormSelectors = field(default_factory=FormSelectors)
    email_sender_pattern: str = "no-reply@fireworks.ai"
    email_subject_pattern: str = "Verify your Fireworks account"
    verification_type: str = "link"
    verification_code_regex: str = r"\b(\d{6})\b"
    verification_link_regex: str = r"https?://app\.fireworks\.ai/signup/confirm\?[^\s\"'<>]+"
    models: list[str] = field(default_factory=list)
    captcha_strategy: str = "none"
    enable_stealth: bool = False


@dataclass
class MailConfig:
    poll_interval_sec: int = 10
    poll_timeout_sec: int = 180
    connect_timeout_sec: int = 30
    # POP 登录连续失败阈值：超过此数判定为假邮箱并禁用（用户要求 >3，默认 3 即第 4 次触发）
    pop_login_fail_threshold: int = 3


@dataclass
class RegistrarConfig:
    concurrency: int = 1
    timeout_sec: int = 300
    max_retry: int = 3
    headless: bool = True
    min_delay_sec: int = 5
    max_delay_sec: int = 15
    proxy_pool: list[str] = field(default_factory=list)


@dataclass
class KaringConfig:
    """Karing 代理客户端配置（Clash 兼容，sing-box 核心）。

    每注册一个 key 通过控制端口切换到一个延迟 <max_latency_ms 的可用节点，
    代理出口端口不变（Playwright 始终走 proxy_out），karing 内部按选中节点转发。
    """
    enabled: bool = True
    api_host: str = "127.0.0.1"
    api_port: int = 3057          # 控制端口（Clash 风格 RESTful API）
    proxy_port: int = 3067        # 代理出口端口（Playwright/HTTP 走这里）
    dashboard_port: int = 3072    # 在线面板端口（Web UI，仅记录/探活）
    cluster_port: int = 3050      # 集群服务端口（仅记录）
    selector_name: str = ""       # Selector 组名，空=自动发现第一个 Selector 组
    secret: str = ""              # 控制端口 secret（karing service.json "secret" 字段），无则 401
    delay_test_url: str = "https://www.gstatic.com/generate_204"
    max_latency_ms: int = 400     # 节点延迟阈值（ms），超过视为不可用
    node_timeout_ms: int = 5000   # 单节点延迟测试超时（ms）
    switch_per_key: bool = True   # 每注册一个 key 换一个节点
    request_timeout: float = 6.0  # 控制端口 HTTP 请求超时（秒）


@dataclass
class PathsConfig:
    # 相对 fw-keypool 项目根（data/ 已移入 fw-keypool 内部）
    state_db: str = "data/state.db"
    keys_json: str = "data/keys.json"
    logs_dir: str = "data/logs"
    screenshots_dir: str = "data/screenshots"


@dataclass
class AppConfig:
    fireworks: FireworksConfig = field(default_factory=FireworksConfig)
    mail: MailConfig = field(default_factory=MailConfig)
    registrar: RegistrarConfig = field(default_factory=RegistrarConfig)
    karing: KaringConfig = field(default_factory=KaringConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    # 来自 .env
    email_pool_file: str = "emails.csv"
    newapi_base_url: str = "http://127.0.0.1:3000"
    newapi_admin_user: str = "root"
    newapi_admin_pass: str = ""
    newapi_access_token: str = ""
    two_captcha_api_key: str = ""

    def abs_path(self, rel: str) -> Path:
        """把 config 里的相对路径转为相对项目根的绝对路径。"""
        p = Path(rel)
        if not p.is_absolute():
            p = (_PROJECT_ROOT / p).resolve()
        return p


def _build_karing(cfg: dict) -> KaringConfig:
    """从 config.yaml 的 karing 段构建 KaringConfig。"""
    return KaringConfig(
        enabled=cfg.get("enabled", KaringConfig.enabled),
        api_host=cfg.get("api_host", KaringConfig.api_host),
        api_port=int(cfg.get("api_port", KaringConfig.api_port)),
        proxy_port=int(cfg.get("proxy_port", KaringConfig.proxy_port)),
        dashboard_port=int(cfg.get("dashboard_port", KaringConfig.dashboard_port)),
        cluster_port=int(cfg.get("cluster_port", KaringConfig.cluster_port)),
        selector_name=cfg.get("selector_name", KaringConfig.selector_name),
        secret=cfg.get("secret", KaringConfig.secret),
        delay_test_url=cfg.get("delay_test_url", KaringConfig.delay_test_url),
        max_latency_ms=int(cfg.get("max_latency_ms", KaringConfig.max_latency_ms)),
        node_timeout_ms=int(cfg.get("node_timeout_ms", KaringConfig.node_timeout_ms)),
        switch_per_key=cfg.get("switch_per_key", KaringConfig.switch_per_key),
        request_timeout=float(cfg.get("request_timeout", KaringConfig.request_timeout)),
    )


def _build_from_yaml(cfg: dict) -> AppConfig:
    fw = cfg.get("fireworks", {}) or {}
    selectors = fw.get("form_selectors", {}) or {}
    mail = cfg.get("mail", {}) or {}
    reg = cfg.get("registrar", {}) or {}
    paths = cfg.get("paths", {}) or {}

    return AppConfig(
        fireworks=FireworksConfig(
            signup_url=fw.get("signup_url", FireworksConfig.signup_url),
            login_url=fw.get("login_url", FireworksConfig.login_url),
            onboarding_url=fw.get("onboarding_url", FireworksConfig.onboarding_url),
            api_keys_url=fw.get("api_keys_url", FireworksConfig.api_keys_url),
            base_url=fw.get("base_url", FireworksConfig.base_url),
            form_selectors=FormSelectors(**{
                k: selectors.get(k, getattr(FormSelectors(), k))
                for k in FormSelectors.__dataclass_fields__
            }),
            email_sender_pattern=fw.get("email_sender_pattern", FireworksConfig.email_sender_pattern),
            email_subject_pattern=fw.get("email_subject_pattern", FireworksConfig.email_subject_pattern),
            verification_type=fw.get("verification_type", FireworksConfig.verification_type),
            verification_code_regex=fw.get("verification_code_regex", r"\b(\d{6})\b"),
            verification_link_regex=fw.get("verification_link_regex", FireworksConfig.verification_link_regex),
            models=fw.get("models", []) or [],
            captcha_strategy=fw.get("captcha_strategy", "none"),
            enable_stealth=fw.get("enable_stealth", FireworksConfig.enable_stealth),
        ),
        mail=MailConfig(
            poll_interval_sec=mail.get("poll_interval_sec", 10),
            poll_timeout_sec=mail.get("poll_timeout_sec", 180),
            connect_timeout_sec=mail.get("connect_timeout_sec", 30),
            pop_login_fail_threshold=mail.get("pop_login_fail_threshold", 3),
        ),
        registrar=RegistrarConfig(
            concurrency=reg.get("concurrency", 1),
            timeout_sec=reg.get("timeout_sec", 300),
            max_retry=reg.get("max_retry", 3),
            headless=reg.get("headless", True),
            min_delay_sec=reg.get("min_delay_sec", 5),
            max_delay_sec=reg.get("max_delay_sec", 15),
            proxy_pool=reg.get("proxy_pool", []) or [],
        ),
        karing=_build_karing(cfg.get("karing", {}) or {}),
        paths=PathsConfig(
            state_db=paths.get("state_db", "data/state.db"),
            keys_json=paths.get("keys_json", "data/keys.json"),
            logs_dir=paths.get("logs_dir", "data/logs"),
            screenshots_dir=paths.get("screenshots_dir", "data/screenshots"),
        ),
    )


@lru_cache(maxsize=1)
def load_config(config_path: str | Path | None = None) -> AppConfig:
    """加载 config.yaml + .env。"""
    # .env 在项目根
    load_dotenv(_PROJECT_ROOT / ".env")
    cfg_path = Path(config_path) if config_path else (Path(__file__).resolve().parent / "config.yaml")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    app = _build_from_yaml(raw or {})
    # .env 覆盖
    app.email_pool_file = os.getenv("EMAIL_POOL_FILE", app.email_pool_file)
    app.registrar.concurrency = int(os.getenv("REGISTRAR_CONCURRENCY", app.registrar.concurrency))
    app.registrar.timeout_sec = int(os.getenv("REGISTRAR_TIMEOUT", app.registrar.timeout_sec))
    app.registrar.max_retry = int(os.getenv("REGISTRAR_MAX_RETRY", app.registrar.max_retry))
    app.registrar.headless = os.getenv("HEADLESS", "true").lower() == "true"
    app.newapi_base_url = os.getenv("NEWAPI_BASE_URL", app.newapi_base_url)
    app.newapi_admin_user = os.getenv("NEWAPI_ADMIN_USER", "root")
    app.newapi_admin_pass = os.getenv("NEWAPI_ADMIN_PASS", "")
    app.newapi_access_token = os.getenv("NEWAPI_ACCESS_TOKEN", "")
    app.two_captcha_api_key = os.getenv("TWO_CAPTCHA_API_KEY", "")
    # ----- 邮件收取（.env 覆盖） -----
    app.mail.pop_login_fail_threshold = int(
        os.getenv("MAIL_POP_LOGIN_FAIL_THRESHOLD", app.mail.pop_login_fail_threshold))
    if os.getenv("HTTP_PROXY") and not app.registrar.proxy_pool:
        app.registrar.proxy_pool = [os.getenv("HTTP_PROXY", "")]
    # ----- Karing 代理控制（.env 覆盖） -----
    app.karing.enabled = os.getenv("KARING_ENABLED", str(app.karing.enabled)).lower() == "true"
    app.karing.api_host = os.getenv("KARING_API_HOST", app.karing.api_host)
    app.karing.api_port = int(os.getenv("KARING_API_PORT", app.karing.api_port))
    app.karing.proxy_port = int(os.getenv("KARING_PROXY_PORT", app.karing.proxy_port))
    app.karing.dashboard_port = int(os.getenv("KARING_DASHBOARD_PORT", app.karing.dashboard_port))
    app.karing.cluster_port = int(os.getenv("KARING_CLUSTER_PORT", app.karing.cluster_port))
    app.karing.selector_name = os.getenv("KARING_SELECTOR_NAME", app.karing.selector_name)
    app.karing.secret = os.getenv("KARING_SECRET", app.karing.secret)
    app.karing.delay_test_url = os.getenv("KARING_DELAY_TEST_URL", app.karing.delay_test_url)
    app.karing.max_latency_ms = int(os.getenv("KARING_MAX_LATENCY_MS", app.karing.max_latency_ms))
    app.karing.node_timeout_ms = int(os.getenv("KARING_NODE_TIMEOUT_MS", app.karing.node_timeout_ms))
    app.karing.switch_per_key = os.getenv("KARING_SWITCH_PER_KEY", str(app.karing.switch_per_key)).lower() == "true"
    return app

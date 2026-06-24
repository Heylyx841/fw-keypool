"""MailFetcher 抽象层：POP3（poplib）+ IMAP（imap_tools）双协议。

用户原话 "popSmtp" → 以 POP3 为主收信；IMAP 作为备选（搜索未读更方便）。
两者统一接口 fetch_verification()：按发件人/主题过滤，轮询直到超时，
返回验证码或验证链接。
"""
from __future__ import annotations

import email
import logging
import poplib
import re
import time
from abc import ABC, abstractmethod
from email.header import decode_header
from typing import Callable

from config import AppConfig
from email_pool import EmailAccount
from verifier_extract import VerificationContent, extract_verification

logger = logging.getLogger(__name__)


class PopLoginError(Exception):
    """POP3/IMAP 登录认证失败（授权码错误/账号不存在等），用于判定假邮箱。

    与一般网络错误区分：认证失败通常意味着凭据无效（假邮箱/错授权码），
    重试无意义；网络错误可能瞬时，应继续轮询。
    """


class FakeEmailError(Exception):
    """邮箱被判定为假邮箱：POP 登录连续失败超过阈值，已禁用。

    抛出后 orchestrator 应 set_email_disabled 并标 FAILED，不再重试注册。
    """

# 邮件解码
def _decode_str(s: str | None) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _get_body(msg: email.message.Message) -> tuple[str, str | None]:
    """返回 (text_body, html_body)。"""
    text = ""
    html = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain" and not text:
                text = decoded
            elif ctype == "text/html" and html is None:
                html = decoded
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html = decoded
            else:
                text = decoded
    return text, html


class MailFetcher(ABC):
    """收信抽象基类。"""

    def __init__(self, account: EmailAccount, app_config: AppConfig,
                 on_pop_login_fail: Callable[[], int] | None = None) -> None:
        self.account = account
        self.cfg = app_config
        # POP 登录失败回调：每次登录失败时调用，返回当前累计失败次数。
        # 由 orchestrator 注入（DB incr_pop_fail），用于跨 run 持久化假邮箱判定。
        self.on_pop_login_fail = on_pop_login_fail

    @abstractmethod
    def _fetch_messages(self) -> list[tuple[str, str, str, str | None]]:
        """返回 [(subject, from_addr, text_body, html_body), ...] 本次拉取的候选邮件。"""

    @abstractmethod
    def _cleanup(self, matched_index: int | None) -> None:
        """命中后清理（IMAP 标记已读/删除，POP3 可不处理）。"""

    def fetch_verification(
        self,
        sender_pattern: str | None = None,
        subject_pattern: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> VerificationContent:
        """轮询拉取邮件，提取验证内容。

        Args:
            sender_pattern: 发件人匹配（子串/正则），None 则不过滤
            subject_pattern: 主题匹配（正则），None 则不过滤
            on_progress: 回调(elapsed_sec, timeout_sec)
        """
        sender_pattern = (sender_pattern or self.cfg.fireworks.email_sender_pattern).lower()
        subject_pattern = subject_pattern or self.cfg.fireworks.email_subject_pattern
        subject_re = re.compile(subject_pattern, re.IGNORECASE)

        threshold = getattr(self.cfg.mail, "pop_login_fail_threshold", 3)
        deadline = time.time() + self.cfg.mail.poll_timeout_sec
        last_err: Exception | None = None
        while time.time() < deadline:
            elapsed = int(self.cfg.mail.poll_timeout_sec - (deadline - time.time()))
            if on_progress:
                on_progress(elapsed, self.cfg.mail.poll_timeout_sec)
            try:
                msgs = self._fetch_messages()
            except PopLoginError as e:
                # POP 登录认证失败：累计计数，超阈值判定假邮箱并中断
                last_err = e
                logger.warning("POP 登录失败 %s: %s", self.account.email, e)
                if self.on_pop_login_fail is not None:
                    count = self.on_pop_login_fail()
                    if count > threshold:
                        logger.error("🚫 [%s] POP 登录连续失败 %d 次（>阈值 %d），判定为假邮箱",
                                     self.account.email, count, threshold)
                        raise FakeEmailError(
                            f"假邮箱判定: {self.account.email} POP 登录连续失败 {count} 次"
                            f"（阈值 {threshold}）") from e
                time.sleep(self.cfg.mail.poll_interval_sec)
                continue
            except Exception as e:  # 网络错误等，继续轮询
                last_err = e
                logger.warning("拉取邮件失败 %s: %s", self.account.email, e)
                time.sleep(self.cfg.mail.poll_interval_sec)
                continue

            for idx, (subject, from_addr, text_body, html_body) in enumerate(msgs):
                if sender_pattern and sender_pattern not in (from_addr or "").lower():
                    continue
                if subject_re.search(subject or "") or sender_pattern in (subject or "").lower():
                    try:
                        vc = extract_verification(
                            subject=subject,
                            from_addr=from_addr,
                            text_body=text_body,
                            html_body=html_body,
                            verification_type=self.cfg.fireworks.verification_type,
                            code_regex=self.cfg.fireworks.verification_code_regex,
                            link_regex=self.cfg.fireworks.verification_link_regex,
                        )
                        self._cleanup(idx)
                        logger.info("命中验证邮件 %s: %s = %s", self.account.email, vc.kind, vc.value[:40])
                        return vc
                    except Exception as e:
                        logger.debug("邮件匹配但提取失败 %s: %s", self.account.email, e)
                        continue
            time.sleep(self.cfg.mail.poll_interval_sec)

        raise TimeoutError(
            f"等待验证邮件超时 {self.account.email}（{self.cfg.mail.poll_timeout_sec}s）"
            + (f"，最后错误: {last_err}" if last_err else "")
        )


class POP3Fetcher(MailFetcher):
    """POP3 收信（Python 标准库 poplib，SSL）。"""

    def _fetch_messages(self) -> list[tuple[str, str, str, str | None]]:
        results: list[tuple[str, str, str, str | None]] = []
        logger.info(
            "POP3 连接 %s:%s（用户 %s）...",
            self.account.host, self.account.port, self.account.username,
        )
        # POP3 SSL，端口默认 995
        try:
            server = poplib.POP3_SSL(
                self.account.host, self.account.port, timeout=self.cfg.mail.connect_timeout_sec
            )
        except Exception as e:
            # 连接阶段失败（网络/端口）：按一般错误继续轮询，不计假邮箱
            raise RuntimeError(f"POP3 连接失败 {self.account.host}:{self.account.port}: {e}") from e
        try:
            try:
                server.user(self.account.username)
                server.pass_(self.account.auth_code)
            except poplib.error_proto as e:
                # 认证失败（授权码错/账号不存在）→ 假邮箱判定信号
                raise PopLoginError(
                    f"POP3 认证失败 {self.account.email}: {e}") from e
            except Exception as e:
                # 登录阶段其他异常（含 socket error）也视为登录失败
                raise PopLoginError(
                    f"POP3 登录异常 {self.account.email}: {e}") from e
            # STAT 返回 (num_msgs, total_size)
            num_msgs, total_size = server.stat()
            logger.info("POP3 登录成功，收件箱共 %d 封邮件（%d 字节）", num_msgs, total_size)
            if num_msgs == 0:
                logger.info("收件箱为空（可能是新邮箱，或邮件尚未到达）")
            # 从最新往前看（验证邮件通常是最近几封）
            scan = min(num_msgs, 10)
            for i in range(num_msgs, max(0, num_msgs - scan), -1):
                resp, lines, _ = server.retr(i)
                raw = b"\r\n".join(lines)
                msg = email.message_from_bytes(raw)
                subject = _decode_str(msg.get("Subject"))
                from_addr = _decode_str(msg.get("From"))
                text, html = _get_body(msg)
                logger.debug("POP3 取信 #%d: subject=%r from=%r", i, subject[:50], from_addr[:50])
                results.append((subject, from_addr, text, html))
        finally:
            try:
                server.quit()
            except Exception:
                pass
        logger.info("POP3 本次拉取 %d 封候选邮件", len(results))
        return results

    def _cleanup(self, matched_index: int | None) -> None:
        # POP3 不主动删除（避免误删用户邮件），留待用户自行清理
        pass


class IMAPFetcher(MailFetcher):
    """IMAP 收信（imap_tools，搜索未读更精准）。"""

    def _fetch_messages(self) -> list[tuple[str, str, str, str | None]]:
        from imap_tools import MailBox  # 延迟导入，可选依赖

        results: list[tuple[str, str, str, str | None]] = []
        # IMAP SSL，端口默认 993
        try:
            mailbox = MailBox(self.account.host, port=self.account.port).login(
                self.account.username, self.account.auth_code
            )
        except Exception as e:
            # IMAP 登录失败（认证错/账号不存在）→ 假邮箱判定信号
            raise PopLoginError(f"IMAP 登录失败 {self.account.email}: {e}") from e
        with mailbox:
            # 取最近未读 + 最近 10 封（兼顾验证邮件可能已读的情况）
            for msg in mailbox.fetch(limit=10, reverse=True, mark_seen=False):
                results.append(
                    (msg.subject or "", msg.from_ or "", msg.text or "", msg.html or None)
                )
        return results

    def _cleanup(self, matched_index: int | None) -> None:
        # IMAP 简单处理：不删除，仅标记已读由 fetch mark_seen 控制
        pass


def make_fetcher(account: EmailAccount, app_config: AppConfig,
                 on_pop_login_fail: Callable[[], int] | None = None) -> MailFetcher:
    """工厂：按 account.protocol 造对应 fetcher。

    on_pop_login_fail: POP/IMAP 登录失败回调，返回当前累计失败次数
    （由 orchestrator 注入 DB incr_pop_fail，用于假邮箱判定）。
    """
    if account.protocol == "pop3":
        return POP3Fetcher(account, app_config, on_pop_login_fail=on_pop_login_fail)
    elif account.protocol == "imap":
        return IMAPFetcher(account, app_config, on_pop_login_fail=on_pop_login_fail)
    raise ValueError(f"未知协议 {account.protocol!r}")

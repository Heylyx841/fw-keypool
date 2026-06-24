"""验证码/验证链接提取。

从邮件正文（text + html）中提取：
- 验证码（verification_type=code）：正则匹配 6 位数字/字母
- 验证链接（verification_type=link）：正则匹配含 verify 的 URL，或 HTML <a href> 第一个链接
"""
from __future__ import annotations

import re
from dataclasses import dataclass

try:
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:  # pragma: no cover
    BeautifulSoup = None  # type: ignore


@dataclass
class VerificationContent:
    """提取结果。kind 为 code/link，value 为验证码字符串或 URL。"""

    kind: str  # "code" | "link"
    value: str
    raw_subject: str = ""
    raw_from: str = ""


class VerificationNotFoundError(Exception):
    """邮件中未找到验证内容。"""


def _extract_code(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text)
    if m:
        # 取第一个捕获组，否则整个匹配
        return m.group(1) if m.groups() else m.group(0)
    return None


def _extract_link(text: str, pattern: str, html: str | None) -> str | None:
    # 优先正则
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        return m.group(1) if m.groups() else m.group(0)
    # 退化：解析 HTML 取第一个 <a href>
    if html and BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        a = soup.find("a", href=True)
        if a:
            return a["href"]
    # 再退化：任意 http(s) 链接
    m = re.search(r"https?://[^\s\"'<>]+", text)
    return m.group(0) if m else None


def extract_verification(
    *,
    subject: str,
    from_addr: str,
    text_body: str,
    html_body: str | None,
    verification_type: str,
    code_regex: str,
    link_regex: str,
) -> VerificationContent:
    """从一封邮件中提取验证内容。

    Args:
        verification_type: "code" 或 "link"
        code_regex / link_regex: 来自 config.yaml 的正则
    """
    combined = text_body or ""
    if html_body:
        # 把 HTML 转纯文本辅助 code 匹配
        if BeautifulSoup:
            combined += "\n" + BeautifulSoup(html_body, "html.parser").get_text(" ", strip=True)
        else:
            combined += "\n" + re.sub(r"<[^>]+>", " ", html_body)

    if verification_type == "code":
        val = _extract_code(combined, code_regex)
        if val:
            return VerificationContent(kind="code", value=val, raw_subject=subject, raw_from=from_addr)
    elif verification_type == "link":
        val = _extract_link(combined, link_regex, html_body)
        if val:
            return VerificationContent(kind="link", value=val, raw_subject=subject, raw_from=from_addr)
    else:
        raise ValueError(f"未知 verification_type: {verification_type!r}")

    raise VerificationNotFoundError("邮件中未找到验证码/链接")

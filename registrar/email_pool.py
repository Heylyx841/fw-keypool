"""邮箱池加载与校验。

支持 CSV / JSON 两种格式，统一解析为 EmailAccount 列表。
支持 Catch-all 域名邮箱的 alias_pattern 批量展开。
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# 常见邮箱服务商默认 POP3/IMAP 配置（auth_code 仍需用户填）
_PROVIDER_DEFAULTS: dict[str, dict[str, dict[str, int]]] = {
    "gmail.com": {"pop3": ("pop.gmail.com", 995), "imap": ("imap.gmail.com", 993)},
    "outlook.com": {"pop3": ("outlook.office365.com", 995), "imap": ("outlook.office365.com", 993)},
    "hotmail.com": {"pop3": ("outlook.office365.com", 995), "imap": ("outlook.office365.com", 993)},
    "qq.com": {"pop3": ("pop.qq.com", 995), "imap": ("imap.qq.com", 993)},
    "163.com": {"pop3": ("pop.163.com", 995), "imap": ("imap.163.com", 993)},
    "126.com": {"pop3": ("pop.126.com", 995), "imap": ("imap.126.com", 993)},
    "yeah.net": {"pop3": ("pop.yeah.net", 995), "imap": ("imap.yeah.net", 993)},
    "sina.com": {"pop3": ("pop.sina.com", 995), "imap": ("imap.sina.com", 993)},
    "foxmail.com": {"pop3": ("pop.qq.com", 995), "imap": ("imap.qq.com", 993)},
}

# 支持 {seq} / {seq:3} / {seq:03d} 等格式（数字宽度 + 可选格式字符 d/s/x/b/o）
_SEQ_RE = re.compile(r"\{seq(?::(\d+)[dbsxoX]?)?\}")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_VALID_PROTOCOLS = {"pop3", "imap"}


class EmailPoolError(ValueError):
    """邮箱池格式或校验错误。"""


@dataclass
class EmailAccount:
    """单个邮箱凭据。

    Attributes:
        email: 注册用邮箱地址（可能是别名）。
        protocol: pop3 或 imap。
        host: 收信服务器主机。
        port: 收信服务器端口。
        username: 登录用户名（部分服务商与 email 不同）。
        auth_code: 授权码/应用专用密码（非登录密码）。
        base_email: 若为别名，指向真实邮箱；否则等于 email。
        note: 备注。
    """

    email: str
    protocol: str
    host: str
    port: int
    username: str
    auth_code: str
    base_email: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        self.protocol = self.protocol.lower()
        if self.protocol not in _VALID_PROTOCOLS:
            raise EmailPoolError(f"不支持的协议 {self.protocol!r}，仅 {sorted(_VALID_PROTOCOLS)}")
        if not _EMAIL_RE.match(self.email):
            raise EmailPoolError(f"邮箱格式非法: {self.email!r}")
        if not self.auth_code:
            raise EmailPoolError(f"auth_code 为空: {self.email}")
        if not self.base_email:
            self.base_email = self.email

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1].lower()


def _expand_alias(pattern: str, count: int) -> Iterator[str]:
    """展开 alias_pattern，生成 count 个别名地址。

    pattern 含 {seq:N} 占位符，N 为零填充宽度（可省略）。
    例如 base+fw{seq:03d}@mydomain.com, count=3 →
        base+fw000@mydomain.com, base+fw001@..., base+fw002@...
    """
    m = _SEQ_RE.search(pattern)
    if not m:
        # 无占位符，单地址
        yield pattern
        return
    width = int(m.group(1)) if m.group(1) else 0
    for i in range(count):
        seq = str(i).zfill(width) if width else str(i)
        yield _SEQ_RE.sub(seq, pattern, count=1)


def _apply_defaults(rec: dict) -> dict:
    """对缺失 host/port 的记录，按邮箱域名补默认值。"""
    email = rec.get("email", "")
    if "@" not in email:
        return rec
    domain = email.rsplit("@", 1)[-1].lower()
    protocol = (rec.get("protocol") or "imap").lower()
    defaults = _PROVIDER_DEFAULTS.get(domain, {}).get(protocol)
    if defaults and (not rec.get("host") or not rec.get("port")):
        host, port = defaults
        rec.setdefault("host", host)
        rec.setdefault("port", port)
    return rec


def _normalize(rec: dict) -> dict:
    """规范化单条记录字段类型。"""
    rec = dict(rec)
    # 跳过注释行/对象
    if any(k.startswith("_") for k in rec) or rec.get("email", "").startswith("#"):
        return {}
    rec["port"] = int(rec["port"]) if rec.get("port") else 0
    rec.setdefault("username", rec.get("email", ""))
    rec.setdefault("auth_code", "")
    rec.setdefault("alias_pattern", "")
    rec.setdefault("note", "")
    return rec


def load_email_pool(path: str | Path, alias_count: int = 0) -> list[EmailAccount]:
    """加载邮箱池文件，返回 EmailAccount 列表。

    Args:
        path: CSV 或 JSON 文件路径。
        alias_count: 若记录含 alias_pattern，展开的别名数量。

    Returns:
        EmailAccount 列表。含 alias_pattern 的记录展开为别名，
        共享同一组 host/port/username/auth_code（base_email 指向原邮箱）。
    """
    p = Path(path)
    if not p.exists():
        raise EmailPoolError(f"邮箱池文件不存在: {p}")
    records: list[dict] = []
    if p.suffix.lower() == ".json":
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise EmailPoolError("JSON 邮箱池需为顶层数组")
        records = [_normalize(r) for r in raw]
    else:
        with p.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                rec = _normalize(row)
                if rec:
                    records.append(rec)

    accounts: list[EmailAccount] = []
    for rec in records:
        if not rec:
            continue
        rec = _apply_defaults(rec)
        alias_pattern = rec.get("alias_pattern", "").strip()
        if alias_pattern and alias_count > 0:
            for alias in _expand_alias(alias_pattern, alias_count):
                accounts.append(
                    EmailAccount(
                        email=alias,
                        protocol=rec["protocol"],
                        host=rec["host"],
                        port=rec["port"],
                        username=rec["username"],
                        auth_code=rec["auth_code"],
                        base_email=rec["email"],
                        note=rec.get("note", ""),
                    )
                )
        else:
            accounts.append(
                EmailAccount(
                    email=rec["email"],
                    protocol=rec["protocol"],
                    host=rec["host"],
                    port=rec["port"],
                    username=rec["username"],
                    auth_code=rec["auth_code"],
                    base_email=rec["email"],
                    note=rec.get("note", ""),
                )
            )
    if not accounts:
        raise EmailPoolError("邮箱池为空或全部被跳过")
    return accounts


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="校验并预览邮箱池")
    ap.add_argument("file", help="emails.csv 或 emails.json 路径")
    ap.add_argument("--alias-count", type=int, default=0, help="别名展开数量")
    args = ap.parse_args()
    for acc in load_email_pool(args.file, args.alias_count):
        print(f"{acc.email:40s} {acc.protocol:5s} {acc.host}:{acc.port} base={acc.base_email}")

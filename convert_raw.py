#!/usr/bin/env python3
"""raw.txt -> emails.csv 转换工具（追加，自动去重）。

raw.txt 格式（每行）:
    [邮箱]----[pop授权码]
    例: your_email@163.com----YOUR_AUTH_CODE

emails.csv 列（与 registrar/email_pool.py 的 EmailAccount 对齐）:
    email,protocol,host,port,username,auth_code,alias_pattern,note

行为:
- 解析 raw.txt，按域名自动匹配 POP3/IMAP 收信服务器默认值。
- 追加到 emails.csv（不覆盖现有记录），按 email 去重。
- emails.csv 不存在时自动创建表头。
- 幂等：重复运行不会产生重复行。

仅依赖 Python 标准库（csv/re/sys/argparse）。
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

# Windows GBK 终端：stdout 重置为 utf-8，避免中文/emoji 崩溃（techContext #9）
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# 常见邮箱服务商 POP3/IMAP 默认配置（与 registrar/email_pool.py._PROVIDER_DEFAULTS 对齐）
PROVIDER_DEFAULTS: dict[str, dict[str, tuple[str, int]]] = {
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

# emails.csv 表头顺序（与 emails.example.csv 一致）
FIELDS = ["email", "protocol", "host", "port", "username", "auth_code", "alias_pattern", "note"]

# raw.txt 行分隔符
RAW_SEP = "----"

# 邮箱格式校验（与 email_pool._EMAIL_RE 对齐）
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def resolve_server(email: str, protocol: str = "pop3") -> tuple[str, int]:
    """按邮箱域名返回 (host, port)，未匹配则默认 pop.{domain}:995。"""
    domain = email.rsplit("@", 1)[-1].lower()
    proto = protocol.lower()
    defaults = PROVIDER_DEFAULTS.get(domain, {}).get(proto)
    if defaults:
        return defaults
    return (f"pop.{domain}", 995)


def parse_raw(raw_path: Path) -> list[tuple[str, str]]:
    """解析 raw.txt，返回 [(email, auth_code), ...]。"""
    entries: list[tuple[str, str]] = []
    for lineno, line in enumerate(raw_path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if RAW_SEP not in line:
            print(f"  [跳过] 第 {lineno} 行格式异常（缺少 '{RAW_SEP}'）: {line!r}", file=sys.stderr)
            continue
        email, _, auth_code = line.partition(RAW_SEP)
        email = email.strip()
        auth_code = auth_code.strip()
        if not EMAIL_RE.match(email):
            print(f"  [跳过] 第 {lineno} 行邮箱格式非法: {email!r}", file=sys.stderr)
            continue
        if not auth_code:
            print(f"  [跳过] 第 {lineno} 行授权码为空: {email!r}", file=sys.stderr)
            continue
        entries.append((email, auth_code))
    return entries


def load_existing(csv_path: Path) -> set[str]:
    """读取 emails.csv 已有 email 集合（小写去重）；文件不存在返回空集。"""
    if not csv_path.exists():
        return set()
    seen: set[str] = set()
    with csv_path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = (row.get("email") or "").strip().lower()
            if email:
                seen.add(email)
    return seen


def append_records(
    csv_path: Path,
    entries: list[tuple[str, str]],
    protocol: str = "pop3",
    note: str = "",
) -> tuple[int, int, int]:
    """追加记录到 emails.csv。返回 (新增, 跳过重复, 源总数)。"""
    existing = load_existing(csv_path)
    write_header = not csv_path.exists()
    added = 0
    skipped = 0
    new_rows: list[dict[str, str]] = []
    for email, auth_code in entries:
        key = email.lower()
        if key in existing:
            skipped += 1
            continue
        host, port = resolve_server(email, protocol)
        new_rows.append({
            "email": email,
            "protocol": protocol.lower(),
            "host": host,
            "port": str(port),
            "username": email,
            "auth_code": auth_code,
            "alias_pattern": "",
            "note": note,
        })
        existing.add(key)
        added += 1

    if new_rows:
        # 追加前确保文件以换行结尾，避免与既有末行拼接（末行缺 \n 时直接 append 会并到同一行）
        if csv_path.exists() and csv_path.stat().st_size > 0:
            with csv_path.open("rb") as fb:
                fb.seek(-1, 2)
                if fb.read(1) not in (b"\n", b"\r"):
                    with csv_path.open("a", encoding="utf-8", newline="") as fpad:
                        fpad.write("\n")
        with csv_path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            if write_header:
                writer.writeheader()
            for row in new_rows:
                writer.writerow(row)

    return added, skipped, len(entries)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="将 raw.txt（[邮箱]----[pop授权码]）转化为 emails.csv 并追加去重。",
    )
    base = Path(__file__).resolve().parent
    parser.add_argument("--raw", default=str(base / "raw.txt"), help="raw.txt 路径（默认 fw-keypool/raw.txt）")
    parser.add_argument("--csv", default=str(base / "emails.csv"), help="emails.csv 路径（默认 fw-keypool/emails.csv）")
    parser.add_argument("--protocol", default="pop3", choices=["pop3", "imap"], help="收信协议（默认 pop3）")
    parser.add_argument("--note", default="", help="备注列内容（默认空）")
    args = parser.parse_args(argv)

    raw_path = Path(args.raw)
    csv_path = Path(args.csv)

    if not raw_path.exists():
        print(f"[错误] raw.txt 不存在: {raw_path}", file=sys.stderr)
        return 1

    print(f"[源] {raw_path}")
    print(f"[目标] {csv_path}")
    print(f"[协议] {args.protocol}")

    entries = parse_raw(raw_path)
    if not entries:
        print("[结果] raw.txt 无有效记录，未改动 emails.csv。")
        return 0

    added, skipped, total = append_records(csv_path, entries, protocol=args.protocol, note=args.note)
    print(f"[结果] 源 {total} 条 | 新增 {added} 条 | 跳过重复 {skipped} 条")
    print(f"[完成] emails.csv 现有 {len(load_existing(csv_path))} 条记录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

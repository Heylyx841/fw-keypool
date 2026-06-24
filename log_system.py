"""日志系统：记录控制台输出（stdout + stderr + logging）并保存到文件，最多留 3 份。

## 功能
- 捕获控制台输出（print / logging 默认 stderr / 异常 traceback）→ 同时写入控制台与日志文件
- 每次运行生成一份带时间戳的日志文件（data/logs/run_YYYYMMDD_HHMMSS.log）
- 自动轮转：日志目录只保留最近 N 份（默认 3），超出删除最旧的
- Windows GBK 终端兼容：tee 写入前对无法编码字符做安全降级

## 用法
    from log_system import setup_console_logging
    setup_console_logging(log_dir="data/logs", max_files=3, verbose=False)
    # 之后所有 print / logging 输出都会被记录到文件 + 仍显示在控制台

## 设计
- TeeStream：包装原 stdout/stderr，write 时同时写原流 + 共享日志文件（带 flush）
- 不依赖第三方库，仅标准库（os/sys/time/datetime/logging/threading/pathlib/re）
- 线程安全：TeeStream 内部用 threading.Lock 保护文件写入
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

# 日志文件名前缀（用于轮转时识别本系统产生的日志文件）
_LOG_PREFIX = "run_"
# 默认保留份数
DEFAULT_MAX_FILES = 3


class TeeStream:
    """同步写入「原流」与「日志文件」的 tee 包装器。

    实现 sys.stdout / sys.stderr 的 stream 接口（write/flush/isatty/fileno/reconfigure），
    使 print() 与 logging（默认输出到 stderr）的输出同时落到控制台与文件。
    线程安全：文件写入加锁，避免多线程交错损坏行。
    """

    def __init__(self, original: Any, log_file: Path, encoding: str = "utf-8") -> None:
        self.original = original
        self.log_file = log_file
        self._encoding = encoding
        self._lock = threading.Lock()
        # 延迟打开文件：避免在 import 阶段创建（由 setup 显式打开）
        self._fh = open(log_file, "a", encoding=encoding, errors="replace", buffering=1)

    # ---- 核心写接口 ----
    def write(self, data: str) -> int:
        if not isinstance(data, str):
            data = str(data)
        # 1. 写原控制台流（容错：GBK 终端无法编码的字符安全替换，避免崩）
        written = 0
        try:
            self.original.write(data)
            written = len(data)
        except UnicodeEncodeError:
            # Windows GBK 终端遇到 emoji/特殊字符：降级用 replace 编码后写
            try:
                enc = getattr(self.original, "encoding", None) or "utf-8"
                safe = data.encode(enc, errors="replace").decode(enc, errors="replace")
                self.original.write(safe)
                written = len(safe)
            except Exception:
                written = 0
        except Exception:
            written = 0
        # 2. 写日志文件（UTF-8，加锁）
        try:
            with self._lock:
                self._fh.write(data)
                self._fh.flush()
        except Exception:
            pass
        return written

    def flush(self) -> None:
        try:
            self.original.flush()
        except Exception:
            pass
        try:
            with self._lock:
                self._fh.flush()
        except Exception:
            pass

    def close(self) -> None:
        try:
            with self._lock:
                self._fh.flush()
                self._fh.close()
        except Exception:
            pass

    # ---- 透传常用流属性，减少对第三方库（如 logging StreamHandler）的破坏 ----
    def isatty(self) -> bool:
        try:
            return self.original.isatty()
        except Exception:
            return False

    def fileno(self) -> int:
        return self.original.fileno()

    @property
    def encoding(self) -> str:
        # logging StreamHandler 会读取 stream.encoding；优先返回原流编码（保证日志格式一致）
        return getattr(self.original, "encoding", None) or self._encoding

    @property
    def errors(self) -> str:
        return getattr(self.original, "errors", "strict") or "strict"

    def reconfigure(self, **kwargs: Any) -> None:
        # 透传 reconfigure（run.py/start.py 会调 sys.stdout.reconfigure(utf-8)）
        try:
            self.original.reconfigure(**kwargs)
        except Exception:
            pass

    def __getattr__(self, name: str) -> Any:
        # 其余属性（如 line_buffering/newlines 等）透传到原流
        return getattr(self.original, name)


def _rotate_logs(log_dir: Path, max_files: int, prefix: str = _LOG_PREFIX) -> int:
    """轮转：删除超出 max_files 的最旧日志文件。返回删除数。

    只处理本系统产生的 {prefix}*.log 文件，按修改时间排序，保留最新的 max_files 份。
    """
    if max_files <= 0:
        return 0
    try:
        logs = sorted(
            [p for p in log_dir.glob(f"{prefix}*.log") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
        )
    except Exception:
        return 0
    to_delete = logs[:-max_files] if len(logs) > max_files else []
    deleted = 0
    for p in to_delete:
        try:
            p.unlink()
            deleted += 1
        except Exception:
            pass
    return deleted


def setup_console_logging(
    log_dir: str | Path = "data/logs",
    max_files: int = DEFAULT_MAX_FILES,
    verbose: bool = False,
    log_level: int | None = None,
    log_name: str | None = None,
) -> Path:
    """初始化控制台日志捕获 + 文件保存 + 轮转。

    参数：
        log_dir: 日志目录（相对项目根或绝对路径）
        max_files: 最多保留的日志份数（默认 3）
        verbose: True=DEBUG，False=INFO
        log_level: 显式日志级别（覆盖 verbose）
        log_name: 自定义日志文件名（不含目录）；默认 run_YYYYMMDD_HHMMSS.log

    返回：本次日志文件的绝对路径。

    副作用：
        - 替换 sys.stdout / sys.stderr 为 TeeStream
        - 配置 logging.basicConfig 输出到 stderr（被 tee 捕获到文件）
        - 轮转删除超出 max_files 的旧日志
    """
    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)

    # Windows GBK 终端：尝试先把原 stdout/stderr 切 utf-8，减少 tee 降级频次
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    # 生成本次日志文件名（含防同秒冲突：若已存在则追加 _2/_3 ...）
    if not log_name:
        base = f"{_LOG_PREFIX}{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        log_name = f"{base}.log"
        log_file = log_dir_path / log_name
        if log_file.exists():
            # 同秒冲突：追加序号直到不冲突
            n = 2
            while log_file.exists():
                log_name = f"{base}_{n}.log"
                log_file = log_dir_path / log_name
                n += 1
    else:
        log_file = log_dir_path / log_name

    # 写文件头（标记本次运行开始）
    try:
        with open(log_file, "w", encoding="utf-8") as fh:
            fh.write(f"==== fw-keypool 日志 {datetime.now().isoformat()} ====\n")
    except Exception:
        pass

    # 包装 stdout / stderr
    sys.stdout = TeeStream(sys.stdout, log_file)  # type: ignore[assignment]
    sys.stderr = TeeStream(sys.stderr, log_file)  # type: ignore[assignment]

    # 配置 logging（输出到 stderr，已被 tee 捕获到文件）
    if log_level is None:
        log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,  # 覆盖已有 basicConfig（多次调用安全）
    )

    # 轮转：本次日志文件已创建并计入，超出 max_files 的最旧文件删除
    deleted = _rotate_logs(log_dir_path, max_files)
    if deleted:
        logging.getLogger(__name__).info("日志轮转：删除 %d 份旧日志（保留 %d 份）", deleted, max_files)

    logging.getLogger(__name__).info(
        "日志系统已启动：文件=%s（最多保留 %d 份，verbose=%s）", log_file, max_files, verbose)
    return log_file.resolve()


def get_log_dir(project_root: str | Path | None = None) -> Path:
    """获取日志目录绝对路径（默认项目根/data/logs）。"""
    root = Path(project_root) if project_root else Path(__file__).resolve().parent
    return (root / "data" / "logs").resolve()


if __name__ == "__main__":
    # 自测：打印若干行 + 验证轮转
    import argparse
    ap = argparse.ArgumentParser(description="日志系统自测")
    ap.add_argument("--dir", default="data/logs", help="日志目录")
    ap.add_argument("--max", type=int, default=DEFAULT_MAX_FILES, help="保留份数")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    lf = setup_console_logging(args.dir, max_files=args.max, verbose=args.verbose)
    print(f"控制台输出测试：本行应同时出现在控制台和 {lf}")
    logging.getLogger("test").info("logging 输出测试")
    logging.getLogger("test").debug("debug 输出（verbose 才可见）")
    # 触发一个 stderr 写入（异常）
    try:
        raise ValueError("测试异常 traceback 是否被记录")
    except Exception:
        logging.exception("捕获测试异常")
    print(f"日志目录现有文件：{sorted(p.name for p in Path(args.dir).glob('run_*.log'))}")

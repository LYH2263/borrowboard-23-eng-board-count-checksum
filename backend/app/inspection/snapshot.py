"""主库 → 临时侧车的快照复制。

只用 SQLite online backup API 逐页复制，源库以普通连接打开但全程不执行
任何写语句；复制完成后源连接立即关闭，对账阶段只以 immutable 只读 URI
打开侧车，物理上杜绝巡检写库。
"""
import hashlib
import sqlite3
import tempfile
from pathlib import Path

from app.db import db_path


def make_snapshot(main: Path | None = None, sidecar_dir: Path | None = None) -> Path:
    """把主库复制到一个临时侧车文件，返回侧车路径。调用方负责删除。"""
    main = main or db_path()
    if not Path(main).exists():
        raise FileNotFoundError(f"主库不存在: {main}")
    fd, tmp = tempfile.mkstemp(
        prefix="borrowboard-sidecar-", suffix=".db",
        dir=str(sidecar_dir) if sidecar_dir else None,
    )
    Path(tmp).unlink(missing_ok=True)  # 交给 sqlite 创建，避免残留空文件
    src = sqlite3.connect(str(main))
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)  # 页级一致性快照（以调用方 src 为源），期间业务可继续写主库
        finally:
            dst.close()
    finally:
        src.close()
    return Path(tmp)


def open_readonly(path: Path) -> sqlite3.Connection:
    """immutable + query_only：连接层面保证对账绝不写侧车。"""
    uri = f"file:{path.as_posix()}?immutable=1&mode=ro"
    c = sqlite3.connect(uri, uri=True)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only = ON")
    return c


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def remove_sidecar(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)

"""Private local submission journal. It stores no authorization or driver identity."""

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import fcntl
import os
import sqlite3


class BusyError(RuntimeError):
    pass


def vehicle_key(plate: str) -> str:
    return sha256(plate.strip().upper().encode()).hexdigest()


def attempt_key(plate: str, permit: str, apply_date: str) -> str:
    return sha256(f"{vehicle_key(plate)}|{permit}|{apply_date}".encode()).hexdigest()


@contextmanager
def exclusive(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = data_dir / "run.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BusyError("已有另一个任务运行，本次跳过") from None
        yield
    finally:
        os.close(fd)


class Journal:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = data_dir / "journal.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        path.chmod(0o600)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS attempts (
            key TEXT PRIMARY KEY,
            vehicle TEXT NOT NULL,
            permit TEXT NOT NULL,
            apply_date TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def get(self, key):
        row = self.db.execute("SELECT * FROM attempts WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def history(self, plate):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM attempts WHERE vehicle=? ORDER BY created_at DESC, key",
            (vehicle_key(plate),),
        )]

    def unresolved(self, plate):
        return [row for row in self.history(plate)
                if row["state"] in {"submitting", "uncertain", "accepted", "verified"}]

    def begin(self, plate, permit, apply_date):
        key = attempt_key(plate, permit, apply_date)
        stamp = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO attempts VALUES (?,?,?,?,?,?,?)",
            (key, vehicle_key(plate), permit, apply_date, "submitting", stamp, stamp),
        )
        self.db.commit() # durable before the external mutation
        return key

    def update(self, key, state):
        self.db.execute("UPDATE attempts SET state=?, updated_at=? WHERE key=?",
                        (state, datetime.now(timezone.utc).isoformat(), key))
        self.db.commit()

    def allow_retry(self, key, plate):
        row = self.get(key)
        if not row or row["vehicle"] != vehicle_key(plate):
            raise ValueError("没有找到当前车辆的该条提交记录")
        if row["state"] in {"verified", "approved", "expired"}:
            raise ValueError("服务端已确认的申请不能解除重复提交保护")
        # Explicit local recovery only after the user has checked the official App.
        self.db.execute("DELETE FROM attempts WHERE key=?", (key,))
        self.db.commit()

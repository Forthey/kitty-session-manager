"""Общий модуль для savesession/loadsession — хранилище сессий kitty.

Сессия = снимок одного инстанса kitty (все его OS-окна с табами). Ключ слота — PID
инстанса; на диске лежит до 10 самых свежих файлов `<pid>.json`. Восстановление
открывает шеллы в сохранённых cwd (без перезапуска foreground-команд).

Чистый stdlib. kitty-бинарь и сокеты резолвятся абсолютно, чтобы работать в окружении
без tty и без ~/.local/bin в PATH (systemd user timer).
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

SCHEMA_VERSION = 1
MAX_SESSIONS = 10
HOME = Path.home()


def run_ls(sockpath: str) -> list[dict]:
    """`kitty @ --to unix:SOCK ls` -> распарсенный список OS-окон инстанса.

    Транспорт через сокет не трогает /dev/tty, поэтому работает без tty."""
    res = subprocess.run(
        [kitty_bin(), "@", "--to", f"unix:{sockpath}", "ls"],
        capture_output=True, text=True, timeout=10,
    )
    if res.returncode != 0:
        raise RuntimeError(f"kitty @ ls failed ({res.returncode}): {res.stderr.strip()}")
    return json.loads(res.stdout)


# --------------------------------------------------------------------------- #
# Построение и разбор сессии
# --------------------------------------------------------------------------- #
def count_tabs(os_windows: list[dict]) -> int:
    return sum(len(ow.get("tabs", [])) for ow in os_windows)


def build_session(os_windows: list[dict], pid: int) -> dict:
    """Из сырого `kitty @ ls` собрать компактный dict сессии (см. схему в плане)."""
    now = _dt.datetime.now().astimezone()
    slim_windows: list[dict] = []
    for ow in os_windows:
        tabs = []
        for tab in ow.get("tabs", []):
            windows = [
                {
                    "cwd": w.get("cwd", str(HOME)),
                    "title": w.get("title", ""),
                    "is_focused": bool(w.get("is_focused")),
                }
                for w in tab.get("windows", [])
            ]
            tabs.append({
                "title": tab.get("title", ""),
                "layout": tab.get("layout", ""),
                "is_active": bool(tab.get("is_active")),
                "windows": windows,
            })
        slim_windows.append({"id": ow.get("id"), "tabs": tabs})
    return {
        "version": SCHEMA_VERSION,
        "saved_at": now.isoformat(timespec="seconds"),
        "saved_at_epoch": int(time.time()),
        "kitty_pid": pid,
        "os_windows": slim_windows,
    }


def session_tab_count(session: dict) -> int:
    return count_tabs(session.get("os_windows", []))


def session_tab_titles(session: dict) -> list[str]:
    titles: list[str] = []
    for ow in session.get("os_windows", []):
        for tab in ow.get("tabs", []):
            titles.append(tab.get("title") or "(без имени)")
    return titles


def iter_tabs(session: dict):
    """Пройтись по всем табам сессии: yield (os_window_index, tab_dict)."""
    for i, ow in enumerate(session.get("os_windows", [])):
        for tab in ow.get("tabs", []):
            yield i, tab


# --------------------------------------------------------------------------- #
# Хранилище
# --------------------------------------------------------------------------- #
def storage_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(HOME / ".local" / "share")
    d = Path(base) / "kitty-sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write_json(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_sessions() -> list[dict]:
    """Все сессии, отсортированы новыми вперёд. К каждой добавлено поле `_path`."""
    out: list[dict] = []
    for path in storage_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        data["_path"] = str(path)
        data["_mtime"] = path.stat().st_mtime
        out.append(data)
    out.sort(key=lambda s: s.get("saved_at_epoch") or s.get("_mtime", 0), reverse=True)
    return out


def load_config() -> dict:
    import tomllib
    base = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
    path = base / "kitty-session-manager" / "config.toml"
    try:
        with path.open("rb") as stream:
            config = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"config {path}: {exc}") from exc
    fields = {
        "kitty": {"executable": str, "socket_path": str},
        "autosave": {"enabled": bool, "interval_seconds": int},
        "save": {"max_processes": int, "min_tabs": int, "min_os_windows": int},
        "storage": {"max_sessions": int},
    }
    for section, entries in fields.items():
        if not isinstance(config.get(section), dict):
            raise ValueError(f"config {section}: missing or invalid section")
        for key, typ in entries.items():
            value = config.get(section, {}).get(key)
            if type(value) is not typ:
                raise ValueError(f"config {section}.{key}: missing or invalid type")
            if typ is int and value <= 0:
                raise ValueError(f"config {section}.{key}: must be positive")
    template = config["kitty"]["socket_path"]
    if not os.path.isabs(template) or template.count("{pid}") != 1 or "{" in template.replace("{pid}", "") or "}" in template.replace("{pid}", ""):
        raise ValueError("config kitty.socket_path: absolute path with one {pid} required")
    return config


def configure(config: dict) -> None:
    global SOCKET_GLOB, _socket_template, _executable
    _socket_template = config['kitty']['socket_path']
    SOCKET_GLOB = glob.escape(_socket_template).replace(glob.escape('{pid}'), '*')
    _executable = config['kitty']['executable']


_socket_template = None
_executable = None
SOCKET_GLOB = None

def kitty_bin() -> str:
    return _executable or shutil.which('kitty') or 'kitty'


def socket_for_pid(pid: int | str) -> str:
    return _socket_template.replace('{pid}', str(pid))


def list_sockets() -> list[tuple[int, str]]:
    prefix, suffix = _socket_template.split('{pid}')
    result = []
    for path in glob.glob(SOCKET_GLOB):
        value = path[len(prefix):len(path)-len(suffix) if suffix else None]
        if value.isdigit():
            result.append((int(value), path))
    return sorted(result)


def save_many(sessions: list[dict], maximum: int) -> None:
    if len(sessions) > maximum:
        raise ValueError('number of snapshots exceeds storage.max_sessions')
    if not sessions:
        return
    folder = storage_dir()
    paths = list(folder.glob('*.json'))
    backups = {p: (p.read_bytes(), p.stat().st_atime_ns, p.stat().st_mtime_ns, p.stat().st_mode) for p in paths}
    targets = {folder / f"{s['kitty_pid']}.json" for s in sessions}
    try:
        for session in sessions:
            _atomic_write_json(folder / f"{session['kitty_pid']}.json", session)
        ordered = sorted(folder.glob('*.json'), key=lambda p: (p in targets, p.stat().st_mtime_ns), reverse=True)
        for stale in ordered[maximum:]:
            stale.unlink()
    except Exception as exc:
        errors = []
        for path in targets - backups.keys():
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                errors.append(f'{path}: {error}')
        for path, (data, atime, mtime, mode) in backups.items():
            try:
                if not path.exists() or path.read_bytes() != data:
                    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.rollback-')
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(data)
                    os.replace(tmp, path)
                os.chmod(path, mode)
                os.utime(path, ns=(atime, mtime))
            except OSError as error:
                errors.append(f'{path}: {error}')
        suffix = f"; rollback incomplete: {', '.join(errors)}" if errors else '; rollback completed'
        raise RuntimeError(f'{exc}{suffix}') from exc

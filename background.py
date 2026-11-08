"""Отчеты об ошибках фонового сохранения."""
import datetime
import os
from pathlib import Path
import subprocess
import traceback
import uuid


def state_dir():
    path = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state') / 'kitty-session-manager'
    path.mkdir(parents=True, exist_ok=True)
    return path


def notify(message):
    try:
        subprocess.run(['notify-send', 'kitty-session-manager', message], check=True, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def failed(stage, exc):
    root = state_dir()
    errors = root / 'errors'
    errors.mkdir(exist_ok=True)
    now = datetime.datetime.now().astimezone()
    path = errors / f"{now.strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex}.txt"
    path.write_text(f"Время: {now.isoformat()}\nЭтап: {stage}\nОшибка и результат отката: {exc}\n\n{''.join(traceback.format_exception(exc))}")
    for old in sorted(errors.glob('*.txt'), reverse=True)[50:]:
        old.unlink()
    marker = root / 'failure-active'
    first = not marker.exists()
    marker.touch()
    if first:
        try:
            notify(str(exc))
        except Exception:
            pass


def succeeded():
    (state_dir() / 'failure-active').unlink(missing_ok=True)


def show_errors():
    from rich.console import Console
    from rich.prompt import IntPrompt
    console = Console()
    paths = sorted((state_dir() / 'errors').glob('*.txt'), reverse=True)
    if not paths:
        console.print('Нет отчетов об ошибках')
        return
    for i, path in enumerate(paths):
        console.print(f'{i}: {path.name}')
    choice = IntPrompt.ask('Номер отчета', default=0)
    if not 0 <= choice < len(paths):
        raise ValueError('Неверный номер отчета')
    console.print(paths[choice].read_text())

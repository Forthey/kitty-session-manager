#!/usr/bin/env python3
"""Установить kitty-session-manager из текущего каталога исходников."""
import argparse
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import tomllib

SOURCE = Path(__file__).resolve().parent
FILES = ('savesession', 'loadsession', 'ksession.py', 'background.py', 'config.toml', 'requirements.txt')


def paths():
    home = Path.home()
    data = Path(os.environ.get('XDG_DATA_HOME') or home / '.local/share') / 'kitty-session-manager'
    config = Path(os.environ.get('XDG_CONFIG_HOME') or home / '.config')
    return home, data, config


def systemctl(*args, check=True):
    return subprocess.run(['systemctl', '--user', *args], check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def snapshot(path):
    if path.is_symlink():
        return ('link', os.readlink(path))
    if path.exists():
        if not path.is_file():
            raise RuntimeError(f'Необычный объект: {path}')
        return ('file', path.read_bytes(), path.stat().st_mode)
    return None


def restore(path, saved):
    if path.is_symlink() or path.exists():
        path.unlink()
    if saved is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if saved[0] == 'link':
        path.symlink_to(saved[1])
    else:
        path.write_bytes(saved[1])
        path.chmod(saved[2])


def owned_link(path, data):
    if not path.is_symlink():
        return not path.exists()
    return path.resolve().is_relative_to((data / 'versions').resolve())


def main(argv=None):
    parser = argparse.ArgumentParser(description='Установка kitty-session-manager')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if args.dry_run:
        print('Будут проверены systemd и конфигурация, подготовлена версия и настроены команды и таймер.')
        return 0
    if not args.yes and input('Установить или обновить kitty-session-manager? [y/N] ').strip().lower() not in ('y', 'yes', 'д', 'да'):
        print('Отменено')
        return 2
    try:
        return install(args.yes)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError, tomllib.TOMLDecodeError) as exc:
        print(f'Ошибка установки: {exc}', file=sys.stderr)
        return 1


def install(yes):
    home, data, cfg = paths()
    systemctl('show-environment')
    commands = [home / '.local/bin' / name for name in ('savesession', 'loadsession')]
    for path in commands:
        if not owned_link(path, data):
            raise RuntimeError(f'Конфликт: {path} принадлежит пользователю')
    current = data / 'current'
    previous = data / 'previous'
    for link in (current, previous):
        if (link.exists() or link.is_symlink()) and not link.is_symlink():
            raise RuntimeError(f'Конфликт: {link}')
    config = cfg / 'kitty-session-manager/config.toml'
    settings = tomllib.loads((config if config.exists() else SOURCE / 'config.toml').read_text())
    for section, fields in {'kitty': {'executable': str, 'socket_path': str}, 'autosave': {'enabled': bool, 'interval_seconds': int}, 'save': {'max_processes': int, 'min_tabs': int, 'min_os_windows': int}, 'storage': {'max_sessions': int}}.items():
        for name, kind in fields.items():
            value = settings[section][name]
            if type(value) is not kind or (kind is int and value <= 0):
                raise ValueError(f'Некорректное значение {section}.{name}')
    template = settings['kitty']['socket_path']
    if not os.path.isabs(template) or template.count('{pid}') != 1 or '{' in template.replace('{pid}', '') or '}' in template.replace('{pid}', ''):
        raise ValueError('Некорректный kitty.socket_path')
    interval = settings['autosave']['interval_seconds']
    enabled = settings['autosave']['enabled']
    if type(interval) is not int or interval <= 0 or type(enabled) is not bool:
        raise ValueError('Некорректные настройки autosave')
    kitty = cfg / 'kitty/kitty.conf'
    kittytext = kitty.read_text() if kitty.exists() else ''
    directives = [line.split('#', 1)[0].strip() for line in kittytext.splitlines()]
    managed = cfg / 'kitty/kitty-session-manager.conf'
    if 'include kitty-session-manager.conf' in directives and managed.is_file() and not managed.is_symlink():
        directives.extend(line.split('#', 1)[0].strip() for line in managed.read_text().splitlines())
    rc = [line for line in directives if line.startswith('allow_remote_control ')]
    sockets = [line for line in directives if line.startswith('listen_on ')]
    expected = settings['kitty']['socket_path'].replace('{pid}', '{kitty_pid}')
    if rc and rc[-1].split(None, 1)[1] not in ('yes', 'socket', 'socket-only'):
        raise RuntimeError('Несовместимый allow_remote_control в kitty.conf')
    if sockets and sockets[-1].split(None, 1)[1] != 'unix:' + expected:
        raise RuntimeError('Несовместимый listen_on в kitty.conf')
    change_kitty = not rc or not sockets
    if change_kitty and not yes:
        change_kitty = input('Добавить настройки remote control в kitty.conf? [y/N] ').strip().lower() in ('y', 'yes', 'д', 'да')
    managed_kitty = cfg / 'kitty/kitty-session-manager.conf'
    if change_kitty and (managed_kitty.exists() or managed_kitty.is_symlink()):
        raise RuntimeError(f'Конфликт с файлом настройки kitty: {managed_kitty}')
    if change_kitty and any(line.startswith(('include ', 'globinclude ')) for line in directives):
        raise RuntimeError('Конфигурация kitty использует include, проверьте настройки вручную')
    if change_kitty and kitty.is_symlink():
        raise RuntimeError(f'Не меняю символьную ссылку {kitty}, настройте kitty вручную')
    unitdir = cfg / 'systemd/user'
    service = unitdir / 'kitty-savesession.service'
    timer = unitdir / 'kitty-savesession.timer'
    # Existing unit files must be owned by this installer.
    for unit in (service, timer):
        if unit.is_symlink() or (unit.exists() and (not unit.is_file() or '# managed by kitty-session-manager' not in unit.read_text())):
            raise RuntimeError(f'Конфликт с существующим unit: {unit}')
    systemd_enabled = systemctl('is-enabled', 'kitty-savesession.timer', check=False).returncode == 0
    systemd_active = systemctl('is-active', 'kitty-savesession.timer', check=False).returncode == 0
    changed = [current, previous, *commands, config, service, timer]
    if change_kitty:
        changed.extend((kitty, managed_kitty))
    saved = {path: snapshot(path) for path in changed}
    version = None
    old_current = os.readlink(current) if current.is_symlink() else None
    old_previous = os.readlink(previous) if previous.is_symlink() else None
    try:
        versions = data / 'versions'
        versions.mkdir(parents=True, exist_ok=True)
        version = Path(tempfile.mkdtemp(prefix='release-', dir=versions))
        (version / '.kitty-session-manager-release').write_text('managed\n')
        for name in FILES:
            shutil.copy2(SOURCE / name, version / name)
        subprocess.run([sys.executable, '-m', 'venv', str(version / 'venv')], check=True)
        python = version / 'venv/bin/python'
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(version / 'requirements.txt')], check=True)
        for name in ('savesession', 'loadsession'):
            path = version / name
            script = version / (name + '.py')
            path.rename(script)
            path.write_text('#!/bin/sh\nexec ' + shlex.quote(str(python)) + ' ' + shlex.quote(str(script)) + ' "$@"\n')
            path.chmod(0o755)
        if not config.exists():
            config.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / 'config.toml', config)
        if change_kitty:
            kitty.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            if not rc:
                lines.append('allow_remote_control socket-only')
            if not sockets:
                lines.append(f'listen_on unix:{expected}')
            managed_kitty.write_text('\n'.join(lines) + '\n')
            with kitty.open('a') as stream:
                if kittytext and not kittytext.endswith('\n'):
                    stream.write('\n')
                stream.write('include kitty-session-manager.conf\n')
        elif not rc or not sockets:
            print(f'Настройте kitty вручную: allow_remote_control socket-only и listen_on unix:{expected}')
        if current.is_symlink():
            current.unlink()
        current.symlink_to(version)
        for path in commands:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                path.unlink()
            path.symlink_to(current / path.name)
        unitdir.mkdir(parents=True, exist_ok=True)
        service.write_text(f'# managed by kitty-session-manager\n[Unit]\nDescription=Save kitty sessions\n[Service]\nType=oneshot\nExecStart={current / "savesession"} --all --background\n')
        timer.write_text(f'# managed by kitty-session-manager\n[Unit]\nDescription=Save kitty sessions periodically\n[Timer]\nOnBootSec=1min\nOnUnitActiveSec={interval}s\n[Install]\nWantedBy=timers.target\n')
        systemctl('daemon-reload')
        systemctl('enable' if enabled else 'disable', '--now', 'kitty-savesession.timer')
        if previous.is_symlink():
            previous.unlink()
        if old_current:
            previous.symlink_to(old_current)
        if old_previous and old_previous != old_current:
            obsolete = Path(old_previous)
            if not obsolete.is_absolute():
                obsolete = previous.parent / obsolete
            if obsolete.resolve().is_relative_to(versions.resolve()) and obsolete.is_dir() and (obsolete / '.kitty-session-manager-release').is_file():
                try:
                    shutil.rmtree(obsolete)
                except OSError as cleanup_error:
                    print(f'Не удалось удалить старую версию {obsolete}: {cleanup_error}', file=sys.stderr)
        print('Установка завершена')
        return 0
    except Exception as exc:
        errors = []
        for path in reversed(changed):
            try:
                restore(path, saved[path])
            except OSError as error:
                errors.append(f'{path}: {error}')
        try:
            systemctl('daemon-reload')
            systemctl('enable' if systemd_enabled else 'disable', 'kitty-savesession.timer')
            systemctl('start' if systemd_active else 'stop', 'kitty-savesession.timer')
        except Exception as error:
            errors.append(f'systemd: {error}')
        if version and not errors:
            shutil.rmtree(version, ignore_errors=True)
        raise RuntimeError(f'{exc}. Откат: {", ".join(errors) if errors else "выполнен"}') from exc


if __name__ == '__main__':
    sys.exit(main())

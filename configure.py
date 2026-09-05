"""Interactive configuration without putting a token into shell history."""
import argparse
import asyncio
import getpass
import os
from pathlib import Path
import re
import tempfile
from dotenv import dotenv_values

def parse_admins(value):
    parts = [p for p in re.split(r'[,;\s]+', value.strip()) if p]
    if not parts or any(not p.isdecimal() or not 0 < int(p) < 2**52 for p in parts):
        raise ValueError('Нужны положительные Telegram ID через запятую.')
    ids = list(dict.fromkeys(int(p) for p in parts))
    if len(ids) != 2:
        raise ValueError('Для этой секции укажите ровно два разных ID: ассистент и главный тренер.')
    return ','.join(map(str, ids))

def updated_text(original, changes):
    lines = original.splitlines()
    for key, value in changes.items():
        lines = [line for line in lines if not re.match(r'^\s*(?:export\s+)?'+re.escape(key)+r'\s*=', line)]
        lines.append(f'{key}={value}')
    return '\n'.join(lines) + '\n'

async def verify_token(token, old_token):
    from aiogram import Bot
    from aiogram.exceptions import TelegramAPIError
    from aiogram.utils.token import TokenValidationError
    try:
        bot = Bot(token)
        async with bot.session:
            user = await bot.get_me()
            hook = await bot.get_webhook_info()
    except (TelegramAPIError, TokenValidationError, OSError):
        raise ValueError('Токен не прошёл проверку Telegram. Настройки не изменены.') from None
    if old_token and token.split(':', 1)[0] != old_token.split(':', 1)[0]:
        raise ValueError('Токен относится к другому боту. Перевыпустите токен прежнего бота в BotFather.')
    if hook.url:
        raise ValueError('У бота включён webhook. Сначала отключите прежнее подключение.')
    return user.username

def save(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.bot-env-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(content)
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--upgrade', action='store_true')
    parser.add_argument('--path', type=Path, default=Path(os.getenv('ENV_FILE', Path.home()/'.config/training-bot/bot.env')))
    args = parser.parse_args()
    original = args.path.read_text(encoding='utf-8') if args.path.exists() else ''
    values = dotenv_values(stream=__import__('io').StringIO(original))
    version_upgrade = args.upgrade and values.get('SETTINGS_VERSION') != '2.4'
    first_install = not values.get('BOT_TOKEN')
    changes = {}
    if version_upgrade:
        changes.update(
            POLL_OFFSET_MINUTES='180',
            GOOGLE_SHEET_ID='1YNdUTiRQZ5q_NFu8dlNGDqTbSV3nmwZseq4ZIwy2s_g',
            GOOGLE_SHEET_NAME='Посещения_bot',
            SETTINGS_VERSION='2.4')
        print('🏀 Подготовлена синхронизация с листом «Посещения_bot».')
    if not args.upgrade or first_install:
        if os.isatty(0):
            old_ids = values.get('TRAINER_IDS') or '440415724'
            print(f'Сейчас администраторы: {old_ids}. Оба администратора получают одинаковые права.')
            while True:
                raw = input('Два Telegram ID через запятую (Enter — сохранить текущие): ').strip()
                if not raw:
                    break
                try:
                    changes['TRAINER_IDS'] = parse_admins(raw)
                    break
                except ValueError as exc:
                    print(exc)
            token = getpass.getpass('Новый токен из BotFather (Enter — сохранить текущий; ввод скрыт): ').strip()
            if token:
                username = asyncio.run(verify_token(token, values.get('BOT_TOKEN')))
                changes['BOT_TOKEN'] = token
                print(f'Токен проверен: @{username}.')
        else:
            print('Для смены токена и второго администратора: ~/.local/bin/training-bot configure')
    if changes:
        save(args.path, updated_text(original, changes))
    print('Настройки готовы. База участников и ответов не изменялась.')

if __name__ == '__main__':
    try:
        main()
    except (ValueError, EOFError, KeyboardInterrupt) as exc:
        print(str(exc) or 'Ввод отменён. Настройки не изменены.')
        raise SystemExit(1)

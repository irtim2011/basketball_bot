"""Same-host singleton and an explicit stop on Telegram polling conflict."""
import asyncio
import hashlib
import logging
from pathlib import Path
from contextlib import contextmanager
from aiogram.exceptions import TelegramConflictError

@contextmanager
def single_instance(token):
    # Ubuntu deployment. Windows remains usable for unit tests.
    import fcntl
    folder = Path.home()/'.cache'/'training-bot'
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=folder/(hashlib.sha256(token.encode()).hexdigest()[:24]+'.lock')
    with path.open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Другая копия этой версии бота уже работает под этим пользователем.')
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(),fcntl.LOCK_UN)

class ConflictGuard:
    def __init__(self, dispatcher):
        self.dp=dispatcher
        self.detected=False
        self.stop_task=None

    async def __call__(self, make_request, bot, method):
        try:
            return await make_request(bot,method)
        except TelegramConflictError:
            if method.__api_method__ == 'getUpdates' and not self.detected:
                self.detected=True
                logging.critical('CONFLICT: another bot process uses the same token. Stop the old copy and restart this service.')
                self.stop_task=asyncio.create_task(self.dp.stop_polling())
            raise


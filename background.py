"""Bounded, supervised work that must not hold a user's conversation lock."""
import asyncio
import logging

tasks = {}

def running(key):
    return key in tasks and not tasks[key].done()

def start(key, factory):
    if running(key):
        return False
    task = asyncio.create_task(factory(), name=str(key))
    tasks[key] = task
    def finished(done):
        if tasks.get(key) is done:
            tasks.pop(key, None)
        if not done.cancelled() and done.exception():
            logging.getLogger(__name__).error("Background job %s failed: %s", key, type(done.exception()).__name__)
    task.add_done_callback(finished)
    return True

async def close():
    pending = list(tasks.values())
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    tasks.clear()


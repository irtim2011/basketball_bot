"""Scheduled and manual polls share durable responses and one delivery lock."""
import asyncio
import logging
from datetime import datetime, timedelta
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import background
import db
import events
import utils
import texts
import google_sheet
from config import POLL_OFFSET_MINUTES
from ui import inline

log = logging.getLogger(__name__)
delivery_lock = asyncio.Lock()

async def deliver(bot,slot,start):
    sent=skipped=failed=0
    for p in await db.get_active_registered_participants():
        current=await events.get_slot(slot['id'])
        participant=await db.get_participant(p['id'])
        if not events.matches(current,start) or utils.now()>=start:
            return sent,skipped,failed,True
        if not participant['is_active']:
            continue
        response=await events.response_for(p['id'],slot['id'],start)
        if response['message_id'] is not None:
            skipped+=1
            continue
        try:
            message=await bot.send_message(p['telegram_id'],
                texts.poll_text(start),
                reply_markup=inline([[('✅ Приду',f"r:{response['id']}:yes"),('❌ Не приду',f"r:{response['id']}:no")]]))
            await db._c().execute('UPDATE responses SET message_id=? WHERE id=?',(message.message_id,response['id']))
            await db._c().commit()
            sent+=1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            await db.set_active(p['id'],False)
            log.warning('Participant %s blocked bot; mailing disabled',p['id'])
        except TelegramRetryAfter as exc:
            await asyncio.sleep(min(exc.retry_after,60))
            return sent,skipped,failed+1,False
        except Exception:
            failed+=1
            log.exception('Delivery failed for participant %s; retry next tick',p['id'])
    if sent or skipped:
        google_sheet.queue()
    return sent,skipped,failed,False

async def tick(bot):
    async with delivery_lock:
        requests=await (await db._c().execute("SELECT * FROM manual_polls WHERE status='pending'")).fetchall()
        manual_keys=set()
        for request in requests:
            start=datetime.fromisoformat(request['starts_at'])
            slot=await events.get_slot(request['schedule_id'])
            manual_keys.add((request['schedule_id'],request['starts_at']))
            if not events.matches(slot,start) or start<=utils.now():
                sent=skipped=failed=0
                cancelled=True
            else:
                sent,skipped,failed,cancelled=await deliver(bot,slot,start)
            if failed and not cancelled:
                continue
            status='cancelled' if cancelled else 'done'
            # Do not mark a newer request completed if another trainer queued one mid-delivery.
            cur=await db._c().execute(
                'UPDATE manual_polls SET status=? WHERE id=? AND requested_at=?',
                (status,request['id'],request['requested_at']))
            await db._c().commit()
            if not cur.rowcount:
                continue
            result='отменён: тренировка перенесена, удалена или уже началась' if cancelled else f'завершён. Новых отправок: {sent}; отправлено ранее: {skipped}'
            try:
                await bot.send_message(request['trainer_id'],f'Опрос на {start:%d.%m.%Y %H:%M} {result}.')
            except Exception:
                log.warning('Could not send manual delivery summary')
        now=utils.now()
        for slot in await db.list_schedule():
            for start in events.occurrences(slot,now,POLL_OFFSET_MINUTES//1440+2):
                if (slot['id'],start.isoformat()) in manual_keys:
                    continue
                if start-timedelta(minutes=POLL_OFFSET_MINUTES)<=now<start:
                    await deliver(bot,slot,start)

def kick(bot):
    background.start('delivery',lambda: tick(bot))

async def scheduled_kick(bot):
    # A synchronous APScheduler job runs in a thread, without an asyncio loop.
    kick(bot)

def setup_scheduler(bot):
    scheduler=AsyncIOScheduler(timezone=utils.TZ)
    # Coalesce interval invocations into the same supervised background job.
    scheduler.add_job(scheduled_kick,'interval',seconds=10,args=[bot],id='delivery',
                      next_run_time=utils.now(),max_instances=1,coalesce=True,
                      executor='default')
    scheduler.start()
    return scheduler

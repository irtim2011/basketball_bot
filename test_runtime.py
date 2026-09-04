import asyncio
import contextlib
import io
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import background
import db
import events
import utils
from interaction import Ingress
from scheduler import tick,setup_scheduler

def update(text=None,callback=None,uid=1):
    user=SimpleNamespace(id=uid)
    msg=SimpleNamespace(from_user=user,chat=SimpleNamespace(id=uid,type='private'),text=text,message_id=7)
    return SimpleNamespace(
        message=msg if callback is None else None,
        callback_query=SimpleNamespace(id=callback,from_user=user,message=msg,data='r:1:yes') if callback else None)

class IngressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await background.close()

    async def test_ack_does_not_wait_for_slow_previous_handler(self):
        ingress=Ingress()
        entered=asyncio.Event()
        release=asyncio.Event()
        acknowledged=asyncio.Event()
        async def ack(*args,**kwargs):
            acknowledged.set()
        bot=SimpleNamespace(id=1,answer_callback_query=ack)
        processed=[]
        async def handler(event,data):
            if event.message:
                entered.set()
                await release.wait()
            processed.append('callback' if event.callback_query else 'message')
        first=asyncio.create_task(ingress(handler,update('/table'),{'bot':bot}))
        await entered.wait()
        second=asyncio.create_task(ingress(handler,update(callback='q1'),{'bot':bot}))
        try:
            await asyncio.wait_for(acknowledged.wait(),0.2)
            self.assertFalse(second.done())
            self.assertEqual(processed,[])
        finally:
            release.set()
            await asyncio.gather(first,second)
        self.assertEqual(processed,['message','callback'])

    async def test_menu_burst_coalesces_and_other_users_are_independent(self):
        ingress=Ingress()
        entered=asyncio.Event()
        release=asyncio.Event()
        seen=[]
        bot=SimpleNamespace(id=1)
        async def handler(event,data):
            text=event.message.text
            if text=='/start':
                entered.set()
                await release.wait()
            seen.append((event.message.from_user.id,text))
        first=asyncio.create_task(ingress(handler,update('/start'),{'bot':bot}))
        await entered.wait()
        pending=[]
        for text in ['/training','/schedule','/training','/participants','/menu']:
            pending.append(asyncio.create_task(ingress(handler,update(text),{'bot':bot})))
            await asyncio.sleep(0)
        await asyncio.wait_for(ingress(handler,update('/id',uid=2),{'bot':bot}),0.2)
        release.set()
        await asyncio.gather(first,*pending)
        self.assertEqual(seen,[(2,'/id'),(1,'/start'),(1,'/menu')])

    async def test_plain_registration_input_is_never_coalesced(self):
        ingress=Ingress()
        entered=asyncio.Event()
        release=asyncio.Event()
        seen=[]
        async def handler(event,data):
            if event.message.text=='/start':
                entered.set()
                await release.wait()
            seen.append(event.message.text)
        bot=SimpleNamespace(id=1)
        first=asyncio.create_task(ingress(handler,update('/start'),{'bot':bot}))
        await entered.wait()
        pending=[asyncio.create_task(ingress(handler,update(text),{'bot':bot}))
                 for text in ('Фамилия Имя','+79991234567','/profile')]
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first,*pending)
        self.assertEqual(seen,['/start','Фамилия Имя','+79991234567','/profile'])

    async def test_repeated_callback_is_acknowledged_but_not_reexecuted(self):
        ingress=Ingress()
        bot=SimpleNamespace(id=1,answer_callback_query=AsyncMock())
        handler=AsyncMock()
        await ingress(handler,update(callback='q1'),{'bot':bot})
        await ingress(handler,update(callback='q2'),{'bot':bot})
        await asyncio.sleep(0)
        handler.assert_awaited_once()
        self.assertEqual(bot.answer_callback_query.await_count,2)

    async def test_excel_upload_does_not_hold_interactive_handler(self):
        from handlers_trainer import table
        doc_started=asyncio.Event()
        finish=asyncio.Event()
        async def document(*args,**kwargs):
            doc_started.set()
            await finish.wait()
        message=SimpleNamespace(from_user=SimpleNamespace(id=123),answer=AsyncMock(),answer_document=document)
        state=SimpleNamespace(clear=AsyncMock())
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'report.xlsx'
            target.write_bytes(b'test export')
            with patch('handlers_trainer.events.summary',new=AsyncMock(return_value=([],[{}]))), \
                 patch('handlers_trainer.build_xlsx',return_value=str(target)), \
                 patch('handlers_trainer.google_sheet.configured',return_value=False):
                await asyncio.wait_for(table(message,state),0.2)
                task=background.tasks[('table',123)]
                await asyncio.wait_for(doc_started.wait(),1)
                self.assertFalse(task.done())
                await table(message,state)
                message.answer.assert_awaited_once()
                finish.set()
                await task
                self.assertFalse(target.exists())

class ManualDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old=db.DB_PATH
        db.DB_PATH=str(Path(self.tmp.name)/'test.db')
        await db.init_db()
        self.now=utils.TZ.localize(datetime(2026,9,7,12,0))
        self.clock=patch('utils.now',return_value=self.now)
        self.clock.start()
        self.pid=await db.register_participant(77,'user','Person','+79991234567')
        await db.set_active(self.pid,True)
        self.slot=await events.save_slot(1,'19:00','2026-09-08')
        self.start=self.now+timedelta(days=1,hours=7)
        self.bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))

    async def asyncTearDown(self):
        await background.close()
        self.clock.stop()
        await db.close_db()
        db._conn=None
        db.DB_PATH=self.old
        self.tmp.cleanup()

    def deliveries(self):
        return [call for call in self.bot.send_message.await_args_list if call.args[0]==77]

    async def test_manual_sends_before_window_and_does_not_duplicate(self):
        await tick(self.bot)
        self.assertEqual(len(self.deliveries()),0)
        await events.queue_manual(self.slot,self.start,1)
        await tick(self.bot)
        self.assertEqual(len(self.deliveries()),1)
        await events.queue_manual(self.slot,self.start,1)
        await tick(self.bot)
        self.assertEqual(len(self.deliveries()),1)
        self.assertEqual((await (await db._c().execute('SELECT status FROM manual_polls')).fetchone())['status'],'done')

    async def test_manual_request_survives_restart(self):
        await events.queue_manual(self.slot,self.start,1)
        await db.close_db()
        await db.init_db()
        await tick(self.bot)
        self.assertEqual(len(self.deliveries()),1)

    async def test_cancelled_or_disabled_does_not_send(self):
        await events.queue_manual(self.slot,self.start,1)
        await events.delete_slot(self.slot)
        await tick(self.bot)
        self.assertEqual(len(self.deliveries()),0)
        slot=await events.save_slot(1,'20:00','2026-09-08')
        await events.queue_manual(slot,self.start+timedelta(hours=1),1)
        await db.set_active(self.pid,False)
        await tick(self.bot)
        self.assertEqual(len(self.deliveries()),0)

    async def test_failure_retries_without_resending_successes(self):
        await events.queue_manual(self.slot,self.start,1)
        self.bot.send_message.side_effect=[RuntimeError('offline'),SimpleNamespace(message_id=8),SimpleNamespace(message_id=9)]
        with self.assertLogs('scheduler',level='ERROR'):
            await tick(self.bot)
        row=await (await db._c().execute('SELECT status FROM manual_polls')).fetchone()
        self.assertEqual(row['status'],'pending')
        await tick(self.bot)
        self.assertEqual(len(self.deliveries()),2)
        row=await (await db._c().execute('SELECT status FROM manual_polls')).fetchone()
        self.assertEqual(row['status'],'done')

    async def test_apscheduler_launches_on_event_loop(self):
        self.clock.stop()
        ran=asyncio.Event()
        async def fake_tick(bot):
            ran.set()
        with patch('scheduler.tick',side_effect=fake_tick):
            scheduler=setup_scheduler(self.bot)
            try:
                await asyncio.wait_for(ran.wait(),1)
            finally:
                scheduler.shutdown()
                await asyncio.sleep(0)


class GoogleSyncQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        import google_sheet
        await google_sheet.close()

    async def test_answer_burst_becomes_one_sheet_sync(self):
        import google_sheet
        with patch('google_sheet.configured',return_value=True), \
             patch('google_sheet.sync_now',new=AsyncMock()) as sync:
            for _ in range(20):
                self.assertTrue(google_sheet.queue())
            await google_sheet._task
            sync.assert_awaited_once()

class ConflictTests(unittest.IsolatedAsyncioTestCase):
    async def test_conflict_stops_polling_once(self):
        from instance import ConflictGuard
        from aiogram.exceptions import TelegramConflictError
        from aiogram.methods import GetUpdates
        dp=SimpleNamespace(stop_polling=AsyncMock())
        guard=ConflictGuard(dp)
        method=GetUpdates()
        call=AsyncMock(side_effect=TelegramConflictError(method=method,message='another copy'))
        with self.assertLogs(level='CRITICAL'):
            for _ in range(2):
                with self.assertRaises(TelegramConflictError):
                    await guard(call,None,method)
        await guard.stop_task
        dp.stop_polling.assert_awaited_once()

class ProcessGuardTests(unittest.TestCase):
    def test_only_matching_token_and_polling_script_is_detected(self):
        import process_guard
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            process=root/'24680'
            cwd=process/'cwd'
            cwd.mkdir(parents=True)
            (cwd/'main.py').write_text('from aiogram import Bot\n# start_polling(',encoding='utf-8')
            (cwd/'.env').write_text('BOT_TOKEN=same-token\n',encoding='utf-8')
            (process/'cmdline').write_bytes(b'/usr/bin/python3\0main.py\0')
            (process/'environ').write_text('',encoding='utf-8')
            (process/'stat').write_text('24680 (python process) '+' '.join(str(i) for i in range(30)))
            (process/'cgroup').write_text('0::/user.slice/user@1000.service/app.slice/code.service\n')
            p=process_guard.inspect_process(24680,'same-token',root)
            self.assertEqual(p['pid'],24680)
            self.assertEqual(p['units'],['code.service'])
            self.assertIsNone(process_guard.inspect_process(24680,'different-token',root))
            (cwd/'main.py').write_text('print("unrelated app")',encoding='utf-8')
            self.assertIsNone(process_guard.inspect_process(24680,'same-token',root))

    def test_parent_service_is_not_stopped(self):
        import process_guard
        p={'pid':24680,'cwd':'/example','script':'/example/main.py','start':'123','units':['code.service']}
        with patch('sys.argv',['guard','--stop-old']), \
             patch('process_guard.discover',side_effect=[[p],[]]), \
             patch('process_guard.inspect_process',side_effect=[p,None,None]), \
             patch('process_guard.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='11111')) as run, \
             patch('process_guard.os.kill') as kill, contextlib.redirect_stdout(io.StringIO()):
            process_guard.main()
        kill.assert_called_once_with(24680,process_guard.signal.SIGTERM)
        self.assertTrue(all('disable' not in c.args[0] for c in run.call_args_list))

"""The complete workbook may leave the bot only in a trainer's private chat."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import handlers_trainer


def message_for(user_id, chat_type='private', chat_id=None):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=user_id if chat_id is None else chat_id, type=chat_type),
        answer=AsyncMock(), answer_document=AsyncMock())


class TableAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.trainers = patch.object(handlers_trainer, 'TRAINER_IDS', {111, 222})
        self.trainers.start()
        self.addCleanup(self.trainers.stop)

    async def test_both_trainers_get_link_before_background_export(self):
        expected = ('https://docs.google.com/spreadsheets/d/'
                    '1YNdUTiRQZ5q_NFu8dlNGDqTbSV3nmwZseq4ZIwy2s_g/edit'
                    '?gid=2056812652#gid=2056812652')
        for uid in (111, 222):
            with self.subTest(uid=uid):
                message = message_for(uid)
                state = SimpleNamespace(clear=AsyncMock())
                with patch('handlers_trainer.google_sheet.configured', return_value=True), \
                     patch('handlers_trainer.google_sheet.sheet_url', return_value=expected.split('?')[0]), \
                     patch('handlers_trainer.background.running', return_value=False), \
                     patch('handlers_trainer.background.start') as start:
                    await handlers_trainer.table(message, state)
                message.answer.assert_awaited_once()
                markup = message.answer.await_args.kwargs['reply_markup']
                self.assertEqual(markup.inline_keyboard[0][0].url, expected)
                self.assertIn(expected, message.answer.await_args.args[0])
                start.assert_called_once()
                message.answer_document.assert_not_awaited()

    async def test_outsiders_and_nonprivate_chats_cannot_start_export(self):
        for uid, kind, chat_id in ((333, 'private', 333),
                                   (111, 'group', -10),
                                   (222, 'supergroup', -20),
                                   (111, 'channel', -30),
                                   (111, 'private', 333)):
            with self.subTest(uid=uid, kind=kind):
                message = message_for(uid, kind, chat_id)
                state = SimpleNamespace(clear=AsyncMock())
                with patch('handlers_trainer.background.start') as start, \
                     patch('handlers_trainer.google_sheet.sync_now', new_callable=AsyncMock) as sync, \
                     patch('handlers_trainer.google_sheet.export_workbook_xlsx') as export:
                    await handlers_trainer.table(message, state)
                    await handlers_trainer.export_table(message)
                start.assert_not_called()
                sync.assert_not_awaited()
                export.assert_not_called()
                message.answer.assert_not_awaited()
                message.answer_document.assert_not_awaited()

    async def test_link_is_available_when_google_sync_is_unconfigured(self):
        message = message_for(111)
        state = SimpleNamespace(clear=AsyncMock())
        with patch('handlers_trainer.google_sheet.configured', return_value=False), \
             patch('handlers_trainer.background.running', return_value=False), \
             patch('handlers_trainer.background.start') as start:
            await handlers_trainer.table(message, state)
        button = message.answer.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
        self.assertEqual(button.url, handlers_trainer.full_workbook_link())
        self.assertIn('резервную', message.answer.await_args.args[0])
        start.assert_called_once()

    async def test_each_trainer_can_still_download_full_xlsx(self):
        for uid in (111, 222):
            with self.subTest(uid=uid), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / 'complete.xlsx'
                target.write_bytes(b'workbook export')
                message = message_for(uid)
                with patch('handlers_trainer.google_sheet.configured', return_value=True), \
                     patch('handlers_trainer.google_sheet.sync_now', new_callable=AsyncMock), \
                     patch('handlers_trainer.google_sheet.export_workbook_xlsx', return_value=str(target)):
                    await handlers_trainer.export_table(message)
                message.answer_document.assert_awaited_once()
                self.assertFalse(target.exists())

    async def test_revoked_permission_prevents_upload_and_removes_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'complete.xlsx'
            target.write_bytes(b'workbook export')
            message = message_for(111)

            def export_then_revoke():
                handlers_trainer.TRAINER_IDS.discard(111)
                return str(target)

            with patch('handlers_trainer.google_sheet.configured', return_value=True), \
                 patch('handlers_trainer.google_sheet.sync_now', new_callable=AsyncMock), \
                 patch('handlers_trainer.google_sheet.export_workbook_xlsx', side_effect=export_then_revoke):
                await handlers_trainer.export_table(message)
            message.answer_document.assert_not_awaited()
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()

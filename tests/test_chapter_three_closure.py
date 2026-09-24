import asyncio
import copy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


# Import the application without loading production credentials or starting polling.
with patch("dotenv.load_dotenv"), patch.dict(os.environ, {
    "DATABASE_URL": "", "MI_CHAT_ID": "", "SANDRA_CHAT_ID": "",
    "TOKEN_NARRADOR": "", "TOKEN_CONTROL": "", "OPENAI_API_KEY": "",
}):
    import main as bot


def chapter_three_data(complete=True):
    data = bot.default_data()
    data.update(sandra_chat_id=123, sent_chapter_openings=[2, 3])
    state = data["state"]
    state.update(current_chapter_number=3, chapter=bot.chapter_label(3), completed_chapters=[1, 2])
    for event in state["required_event_progress"]["3"]["events"].values():
        event.update(status="cumplido", evidence="Hecho jugado y registrado.")
    for beat in state["chapter_scene_progress"]["3"]["beats"].values():
        beat.update(status="cumplido", evidence="Escena jugada y registrada.")
    if not complete:
        state["required_event_progress"]["3"]["events"]["decide_investigar"].update(
            status="pendiente", evidence="",
        )
    state["chapter3_handoff"] = {"investigation_decision": {"action": "Seguir la voz"}}
    data["history"] = [{"role": "Narrador", "text": "La voz te despierta.", "chapter_number": 3}]
    return data


class ChapterThreeClosureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.bot = SimpleNamespace(send_message=AsyncMock(), send_chat_action=AsyncMock())
        replacements = {
            "DATA_FILE": root / "data.json",
            "MEMORY_MD_PATH": root / "memory.md",
            "DATABASE_URL": None,
            "DB_READY": False,
            "narrador_app": SimpleNamespace(bot=self.bot),
            "sandra_message_buffers": {},
            "sandra_message_tasks": {},
            "sandra_turn_lock": asyncio.Lock(),
            "send_admin": AsyncMock(),
            "generate_scene": AsyncMock(),
            "generate_chapter_summary": AsyncMock(return_value="- Sandra decide seguir la voz."),
            "send_telegram_text": AsyncMock(),
        }
        for name, value in replacements.items():
            p = patch.object(bot, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(bot, "openai_available", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def closing_scene(self, transition=None):
        return {
            "reply": "Te detienes ante la puerta cerrada.",
            "state": {"required_event_progress": {"3": {"events": {
                "decide_investigar": {"status": "cumplido", "evidence": "Sandra decide seguir la voz."},
            }}}},
            "chapter_transition": transition,
        }

    async def turn(self, text="Me levanto para buscar la voz"):
        bot.sandra_message_buffers[123] = [text]
        await bot.process_sandra_message_batch(123)

    def test_requires_every_event_and_scene_threshold(self):
        data = chapter_three_data()
        state = data["state"]
        self.assertIsNotNone(bot.automatic_chapter_three_transition(state))
        for key in state["required_event_progress"]["3"]["events"]:
            with self.subTest(event=key):
                incomplete = copy.deepcopy(state)
                incomplete["required_event_progress"]["3"]["events"][key]["status"] = "pendiente"
                self.assertIsNone(bot.automatic_chapter_three_transition(incomplete))
        state["chapter_scene_progress"] = bot.default_chapter_scene_progress()
        self.assertIsNone(bot.automatic_chapter_three_transition(state))

    def test_other_chapters_are_unchanged(self):
        state = chapter_three_data()["state"]
        for chapter in (1, 2, 4, 11):
            state["current_chapter_number"] = chapter
            self.assertIsNone(bot.automatic_chapter_three_transition(state))

    def test_recovery_is_durable_idempotent_and_preserves_memory(self):
        data = chapter_three_data()
        data["paused"] = True
        original = copy.deepcopy(data)
        bot.save_data(data)
        self.assertTrue(bot.recover_chapter_three_closure(data))
        saved = bot.load_data()
        self.assertTrue(saved["paused"])
        self.assertTrue(saved["chapter_review_pause"]["requires_manual_resume"])
        self.assertEqual(saved["state"]["current_chapter_number"], 3)
        self.assertEqual(saved["state"]["completed_chapters"], [1, 2, 3])
        self.assertEqual(saved["history"], original["history"])
        self.assertEqual(saved["state"]["chapter3_handoff"], original["state"]["chapter3_handoff"])
        self.assertEqual(saved["chapter_summaries"][0]["state_snapshot"], original["state"])
        self.assertFalse(bot.recover_chapter_three_closure(saved))
        self.assertEqual(bot.load_data(), saved)
        bot.generate_scene.assert_not_awaited()
        bot.generate_chapter_summary.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    def test_recovery_preserves_existing_summary(self):
        bot.save_data(chapter_three_data())
        bot.save_chapter_summary(3, bot.CHAPTER_TITLES[3], "Resumen existente")
        data = bot.load_data()
        bot.recover_chapter_three_closure(data)
        self.assertEqual(bot.chapter_export_context(3)[1], "Resumen existente")

    def test_recovery_stays_locked_when_summary_fails(self):
        data = chapter_three_data()
        bot.save_data(data)
        with patch.object(bot, "save_chapter_summary", side_effect=RuntimeError("test failure")):
            self.assertTrue(bot.recover_chapter_three_closure(data))
        self.assertTrue(bot.load_data()["chapter_review_pause"]["active"])

    async def test_automatic_close_without_model_transition(self):
        for transition in (None, {"completed": False}, {"completed": True, "completed_chapter": 4}):
            with self.subTest(transition=transition):
                bot.save_data(chapter_three_data(complete=False))
                bot.generate_scene.return_value = self.closing_scene(transition)
                await self.turn()
                saved = bot.load_data()
                self.assertTrue(saved["chapter_review_pause"]["requires_manual_resume"])
                self.assertEqual(saved["state"]["current_chapter_number"], 3)
                self.assertIn(3, saved["state"]["completed_chapters"])
                self.assertIn("CAPÍTULO 3 TERMINADO", bot.send_telegram_text.call_args.kwargs["text"])
                self.assertTrue(saved["chapter_summaries"])

    async def test_no_premature_close_when_model_requests_it(self):
        bot.save_data(chapter_three_data(complete=False))
        bot.generate_scene.return_value = {
            "reply": "La voz aguarda.", "state": {},
            "chapter_transition": {"completed": True, "completed_chapter": 3},
        }
        await self.turn("Escucho desde la cama")
        saved = bot.load_data()
        self.assertFalse(saved.get("chapter_review_pause"))
        self.assertNotIn(3, saved["state"]["completed_chapters"])

    async def test_existing_completed_state_blocks_new_turn_and_pending_fiction(self):
        data = chapter_three_data()
        data["pending_narrator_delivery"] = {"text": "Continuacion no enviada", "chapter_number": 3}
        bot.save_data(data)
        await self.turn("Abro la puerta")
        bot.generate_scene.assert_not_awaited()
        bot.send_telegram_text.assert_not_awaited()
        self.assertIn("CAPÍTULO 3 TERMINADO", self.bot.send_message.call_args.kwargs["text"])
        self.assertEqual(bot.load_data()["history"], data["history"])
        self.assertFalse(await bot.deliver_pending_narrator_reply())

    async def test_manual_pause_during_generation_discards_response(self):
        bot.save_data(chapter_three_data(complete=False))

        async def generate(_text):
            data = bot.load_data()
            data["paused"] = True
            bot.save_data(data)
            return self.closing_scene()

        bot.generate_scene.side_effect = generate
        await self.turn()
        bot.send_telegram_text.assert_not_awaited()
        self.assertTrue(bot.load_data()["paused"])

    async def test_closing_delivery_can_be_retried_without_ai(self):
        bot.save_data(chapter_three_data(complete=False))
        bot.generate_scene.return_value = self.closing_scene()
        bot.send_telegram_text.side_effect = bot.TelegramTextDeliveryError(0, 1, RuntimeError("test"))
        await self.turn()
        self.assertTrue(bot.load_data()["pending_narrator_delivery"])
        bot.send_telegram_text.side_effect = None
        self.assertTrue(await bot.deliver_pending_narrator_reply())
        saved = bot.load_data()
        self.assertTrue(saved["chapter_review_pause"]["requires_manual_resume"])
        self.assertIsNone(saved["pending_narrator_delivery"])
        bot.generate_scene.assert_awaited_once()

    async def test_queued_turn_cannot_run_past_closing_turn(self):
        bot.save_data(chapter_three_data(complete=False))
        started, release = asyncio.Event(), asyncio.Event()

        async def generate(_text):
            started.set()
            await release.wait()
            return self.closing_scene()

        bot.generate_scene.side_effect = generate
        first = asyncio.create_task(self.turn())
        await started.wait()
        second = asyncio.create_task(self.turn("Entro"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        bot.generate_scene.assert_awaited_once()
        self.assertIn("CAPÍTULO 3 TERMINADO", self.bot.send_message.call_args.kwargs["text"])

    async def test_resume_does_not_bypass_recovered_gate(self):
        bot.save_data(chapter_three_data())
        chat = SimpleNamespace(send_message=AsyncMock())
        update = SimpleNamespace(effective_chat=chat)
        with patch.object(bot, "is_admin", return_value=True):
            await bot.cmd_reanudar(update, SimpleNamespace())
        self.assertIn("No he reanudado", chat.send_message.call_args.args[0])
        self.assertTrue(bot.load_data()["chapter_review_pause"]["active"])
        self.bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

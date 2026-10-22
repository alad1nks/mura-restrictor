import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Message, Update
from telegram.error import Forbidden, NetworkError, RetryAfter

from mura_restrictor.bot import Settings, build_application, moderate, should_delete


def message(username="username", forwarded=True, chat_type="supergroup", **extra):
    data = {
        "message_id": 5, "date": 1700000000,
        "chat": {"id": -100123, "type": chat_type},
        "from": {"id": 42, "is_bot": False, "first_name": "Member", "username": username},
        "text": "test",
    }
    if forwarded:
        data["forward_origin"] = {"type": "hidden_user", "date": 1700000000, "sender_user_name": "Someone"}
    data.update(extra)
    return Message.de_json(data, None)


class RuleTests(unittest.TestCase):
    def test_decisions(self):
        cases = [
            (message(), True),
            (message(username="UsErNaMe"), True),
            (message(forwarded=False), False),
            (message(username="other"), False),
            (message(username=None), False),
            (message(chat_type="private"), False),
            (message(chat_type="channel"), False),
            (message(chat_type="group"), True),
            (message(sender_chat={"id": -100123, "type": "supergroup"}), False),
            (message(**{"from": None}), False),
        ]
        for msg, expected in cases:
            with self.subTest(msg=msg):
                self.assertEqual(should_delete(msg, Settings("token")), expected)

    def test_original_author_does_not_select_sender(self):
        origin = {"type": "user", "date": 1700000000, "sender_user": {
            "id": 42, "first_name": "Target", "username": "username", "is_bot": False,
        }}
        self.assertFalse(should_delete(message(username="other", forward_origin=origin), Settings("token")))

    def test_all_origin_types(self):
        origins = [
            {"type": "user", "sender_user": {"id": 99, "first_name": "Other", "is_bot": False}},
            {"type": "chat", "sender_chat": {"id": -99, "type": "supergroup"}},
            {"type": "channel", "chat": {"id": -99, "type": "channel"}, "message_id": 7},
        ]
        for origin in origins:
            with self.subTest(origin=origin):
                self.assertTrue(should_delete(message(forward_origin={**origin, "date": 1700000000}), Settings("token")))

    def test_id_takes_priority(self):
        self.assertTrue(should_delete(message(username="renamed"), Settings("token", user_id=42)))
        self.assertFalse(should_delete(message(), Settings("token", user_id=99)))

    def test_configuration(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token", "TARGET_USERNAME": "@username"}, clear=True):
            self.assertEqual(Settings.from_env(), Settings("token"))
        for env in ({}, {"TELEGRAM_BOT_TOKEN": "token", "TARGET_USER_ID": "no"},
                    {"TELEGRAM_BOT_TOKEN": "token", "TARGET_USER_ID": "-1"},
                    {"TELEGRAM_BOT_TOKEN": "token", "TARGET_USERNAME": ""}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env()

    def test_handler_filter(self):
        app = build_application(Settings("123456:fake-token"))
        handler = app.handlers[0][0]
        self.assertTrue(handler.check_update(Update(1, message=message())))
        self.assertTrue(handler.check_update(Update(2, edited_message=message())))
        self.assertFalse(handler.check_update(Update(3, message=message(forwarded=False))))
        self.assertFalse(handler.check_update(Update(4, message=message(chat_type="private"))))


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_only_target_forwards(self):
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"settings": Settings("token")}))
        with patch.object(Message, "delete", new_callable=AsyncMock) as delete:
            for msg in (message(), message(forwarded=False), message(username="other")):
                await moderate(Update(1, message=msg), context)
            delete.assert_awaited_once()

    async def test_permission_error_is_handled(self):
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"settings": Settings("token")}))
        with patch.object(Message, "delete", new_callable=AsyncMock, side_effect=Forbidden("no rights")) as delete:
            with self.assertLogs("mura_restrictor.bot", level="ERROR"):
                await moderate(Update(1, message=message()), context)
            delete.assert_awaited_once()

    async def test_transient_errors_retry(self):
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"settings": Settings("token")}))
        with patch.object(Message, "delete", new_callable=AsyncMock, side_effect=[RetryAfter(1), NetworkError("offline"), True]) as delete:
            with patch("mura_restrictor.bot.asyncio.sleep", new_callable=AsyncMock):
                await moderate(Update(1, message=message()), context)
            self.assertEqual(delete.await_count, 3)

    async def test_retry_limit(self):
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"settings": Settings("token")}))
        with patch.object(Message, "delete", new_callable=AsyncMock, side_effect=NetworkError("offline")) as delete:
            with patch("mura_restrictor.bot.asyncio.sleep", new_callable=AsyncMock), self.assertLogs("mura_restrictor.bot", level="ERROR"):
                await moderate(Update(1, message=message()), context)
            self.assertEqual(delete.await_count, 3)

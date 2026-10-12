import json
import unittest
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.request import ProxyHandler, Request

from assistente.grindr import (
    _NoRedirectHandler,
    classify_exchange,
    classify_question,
    create_local_reply_draft,
    training_lesson,
)


class GrindrTrainingTests(unittest.TestCase):
    def test_question_classifier_returns_only_generic_question_categories(self):
        self.assertEqual(classify_question("Onde você mora?"), "location")
        self.assertEqual(classify_question("Você está disponível?"), "availability")
        self.assertIsNone(classify_question("Estou bem, obrigado."))

    def test_exchange_classifier_keeps_only_allowlisted_categories_and_styles(self):
        exchange = classify_exchange(
            [
                {"direction": "received", "text": "De onde você é, Ana?"},
                {"direction": "sent", "text": "Oi, atendo perto daqui."},
            ]
        )

        if exchange is None:
            self.fail("Expected the visible incoming message and reply to form an exchange.")
        self.assertEqual(exchange, ("location", "warm"))
        topic, lesson = training_lesson(exchange)
        self.assertEqual(topic, "Grindr: Pergunta sobre localização")
        self.assertEqual(lesson, "Use respostas cordiais e naturais.")
        self.assertNotIn("Ana", topic + lesson)
        self.assertNotIn("perto daqui", topic + lesson)

    def test_classifier_requires_a_received_message_followed_by_a_sent_reply(self):
        self.assertIsNone(classify_exchange([{"direction": "received", "text": "Oi, tudo bem?"}]))
        self.assertIsNone(
            classify_exchange(
                [
                    {"direction": "sent", "text": "Tudo certo."},
                    {"direction": "received", "text": "De onde?"},
                ]
            )
        )

    def test_reply_draft_rejects_non_loopback_ollama_before_network_access(self):
        snapshot = {
            "messages": [{"direction": "received", "text": "Oi"}],
        }
        with patch("assistente.grindr.urllib.request.build_opener") as build_opener:
            with self.assertRaisesRegex(ValueError, "Ollama local"):
                create_local_reply_draft(
                    snapshot,
                    "https://example.com",
                    "qwen2.5:3b",
                )
        build_opener.assert_not_called()

    def test_reply_draft_uses_direct_loopback_transport_and_blocks_redirects(self):
        snapshot = {
            "messages": [
                {"direction": "received", "text": "De onde você é?"},
            ],
        }
        response = BytesIO(json.dumps({"message": {"content": "Atendo na Glória."}}).encode())
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch("assistente.grindr.urllib.request.build_opener", return_value=opener) as build:
            draft = create_local_reply_draft(
                snapshot,
                "http://127.0.0.1:11434",
                "qwen2.5:3b",
                ["Grindr: Pergunta sobre localização: Use respostas curtas e diretas."],
            )

        self.assertEqual(draft, "Atendo na Glória.")
        handlers = build.call_args.args
        self.assertTrue(any(isinstance(handler, ProxyHandler) for handler in handlers))
        self.assertEqual(
            next(
                getattr(handler, "proxies")
                for handler in handlers
                if isinstance(handler, ProxyHandler)
            ),
            {},
        )
        self.assertTrue(any(isinstance(handler, _NoRedirectHandler) for handler in handlers))
        request = opener.open.call_args.args[0]
        self.assertIsInstance(request, Request)
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
        payload = json.loads(request.data)
        self.assertIn("De onde você é?", payload["messages"][1]["content"])
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 45)

    def test_redirect_handler_refuses_redirects(self):
        handler = _NoRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(
                Request("http://127.0.0.1:11434"),
                None,
                302,
                "Found",
                {},
                "https://example.com",
            )
        )


if __name__ == "__main__":
    unittest.main()

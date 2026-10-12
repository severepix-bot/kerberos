import json
import shlex
import struct
import subprocess
import unittest
from unittest.mock import patch

from assistente.android import (
    GRINDR_PACKAGE,
    AndroidEmulator,
    AndroidNode,
    OCRRegion,
    OCRScreen,
)


class AndroidDeviceTests(unittest.TestCase):
    def setUp(self):
        self.android = AndroidEmulator()

    def test_foreground_reads_resumed_activity(self):
        output = (
            "topResumedActivity=ActivityRecord{310726 u0 "
            "com.grindrapp.android/.ui.home.HomeActivity} t87}"
        )
        with patch.object(self.android, "_run", return_value=output) as run:
            package = self.android._foreground("127.0.0.1:5685")

        self.assertEqual(package, GRINDR_PACKAGE)
        run.assert_called_once_with(
            ["-s", "127.0.0.1:5685", "shell", "dumpsys", "activity", "activities"]
        )

    def test_foreground_falls_back_to_focused_window(self):
        window_output = (
            "mCurrentFocus=Window{2d07dcf u0 "
            "com.grindrapp.android/com.grindrapp.android.HomeActivityOriginal}"
        )
        with patch.object(
            self.android,
            "_run",
            side_effect=["No resumed activity", window_output],
        ) as run:
            package = self.android._foreground("127.0.0.1:5685")

        self.assertEqual(package, GRINDR_PACKAGE)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["-s", "127.0.0.1:5685", "shell", "dumpsys", "window", "windows"],
        )

    def test_run_only_accepts_explicit_nonzero_exit_codes(self):
        failure = subprocess.CompletedProcess(
            [],
            137,
            "",
            "UI hierchary dumped to: /data/local/tmp/screen.xml",
        )
        with patch("assistente.android.subprocess.run", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "ADB falhou \\(137\\)"):
                self.android._run(["-s", "127.0.0.1:5685", "shell", "uiautomator"])

        with patch("assistente.android.subprocess.run", return_value=failure):
            output = self.android._run(
                ["-s", "127.0.0.1:5685", "shell", "uiautomator"],
                accepted_returncodes=(137,),
            )

        self.assertEqual(output, "UI hierchary dumped to: /data/local/tmp/screen.xml")

    def test_hierarchy_reads_temporary_xml_file_and_removes_it(self):
        hierarchy = (
            '<?xml version="1.0"?><hierarchy><node text="Ana" content-desc="" '
            'resource-id="" class="android.widget.TextView" clickable="false" '
            'enabled="true" bounds="[0,0][100,40]" /></hierarchy>'
        )
        with patch.object(self.android, "_run", side_effect=["", hierarchy, ""]) as run:
            nodes = self.android._hierarchy("127.0.0.1:5685")

        self.assertEqual([node.label for node in nodes], ["Ana"])
        dump_args = run.call_args_list[0].args[0]
        read_args = run.call_args_list[1].args[0]
        cleanup_args = run.call_args_list[2].args[0]
        remote_path = dump_args[-1]
        self.assertTrue(remote_path.startswith("/data/local/tmp/cerebro-accessibility-"))
        self.assertTrue(remote_path.endswith(".xml"))
        self.assertEqual(
            dump_args[3:],
            [
                "timeout",
                "-k",
                "2s",
                "15s",
                "uiautomator",
                "dump",
                remote_path,
            ],
        )
        self.assertEqual(run.call_args_list[0].kwargs["timeout"], 25)
        self.assertEqual(run.call_args_list[0].kwargs["accepted_returncodes"], (137,))
        self.assertEqual(read_args, ["-s", "127.0.0.1:5685", "shell", "cat", remote_path])
        self.assertEqual(
            cleanup_args,
            ["-s", "127.0.0.1:5685", "shell", "rm", "-f", remote_path],
        )

    def test_hierarchy_cleans_temporary_file_when_read_fails(self):
        with patch.object(
            self.android,
            "_run",
            side_effect=[
                "",
                RuntimeError("read failed"),
                "",
                "",
                RuntimeError("read failed"),
                "",
            ],
        ) as run, patch("assistente.android.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "read failed"):
                self.android._hierarchy("127.0.0.1:5685")

        self.assertEqual(run.call_count, 6)
        self.assertEqual(run.call_args_list[2].args[0][3:5], ["rm", "-f"])
        self.assertEqual(run.call_args_list[5].args[0][3:5], ["rm", "-f"])

    def test_hierarchy_retries_once_when_remote_dump_does_not_create_xml(self):
        hierarchy = (
            '<?xml version="1.0"?><hierarchy><node text="Ana" content-desc="" '
            'resource-id="" class="android.widget.TextView" clickable="false" '
            'enabled="true" bounds="[0,0][100,40]" /></hierarchy>'
        )
        with (
            patch.object(
                self.android,
                "_run",
                side_effect=[
                    "",
                    RuntimeError(
                        "ADB falhou (1): cat: /data/local/tmp/cerebro-accessibility.xml: "
                        "No such file or directory"
                    ),
                    RuntimeError(
                        "ADB falhou (1): cat: /data/local/tmp/cerebro-accessibility.xml: "
                        "No such file or directory"
                    ),
                    RuntimeError(
                        "ADB falhou (1): cat: /data/local/tmp/cerebro-accessibility.xml: "
                        "No such file or directory"
                    ),
                    "",
                    "",
                    hierarchy,
                    "",
                ],
            ) as run,
            patch("assistente.android.time.sleep") as sleep,
        ):
            nodes = self.android._hierarchy("127.0.0.1:5685")

        self.assertEqual([node.label for node in nodes], ["Ana"])
        self.assertEqual(run.call_count, 8)
        self.assertEqual(sleep.call_count, 3)

    def test_hierarchy_retries_read_when_dump_file_appears_late(self):
        hierarchy = (
            '<?xml version="1.0"?><hierarchy><node text="Ana" content-desc="" '
            'resource-id="" class="android.widget.TextView" clickable="false" '
            'enabled="true" bounds="[0,0][100,40]" /></hierarchy>'
        )
        missing_file = RuntimeError(
            "ADB falhou (1): cat: /data/local/tmp/cerebro-accessibility.xml: "
            "No such file or directory"
        )
        with (
            patch.object(
                self.android,
                "_run",
                side_effect=["", missing_file, hierarchy, ""],
            ) as run,
            patch("assistente.android.time.sleep") as sleep,
        ):
            nodes = self.android._hierarchy("127.0.0.1:5685")

        self.assertEqual([node.label for node in nodes], ["Ana"])
        self.assertEqual(run.call_count, 4)
        sleep.assert_called_once_with(0.5)

    def test_ocr_screen_keeps_screenshot_in_memory_and_validates_vision_data(self):
        screenshot = (
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 540, 960) + b"local-image"
        )
        vision_data = json.dumps(
            [
                {
                    "text": "Caixa de entrada",
                    "confidence": 0.98,
                    "x": 0.1,
                    "y": 0.9,
                    "width": 0.4,
                    "height": 0.03,
                }
            ]
        ).encode()
        with (
            patch.object(self.android, "_adb", return_value="/opt/homebrew/bin/adb"),
            patch("assistente.android.shutil.which", return_value="/usr/bin/swift"),
            patch(
                "assistente.android.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess([], 0, screenshot, b""),
                    subprocess.CompletedProcess([], 0, vision_data, b""),
                ],
            ) as run,
        ):
            screen = self.android._ocr_screen("127.0.0.1:5685")

        self.assertEqual((screen.width, screen.height), (540, 960))
        self.assertEqual(screen.regions[0].text, "Caixa de entrada")
        self.assertEqual(run.call_args_list[1].kwargs["input"], screenshot)
        self.assertTrue(run.call_args_list[1].args[0][-1].endswith("vision_ocr.swift"))

    def test_ocr_screen_rejects_invalid_coordinates(self):
        screenshot = (
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 540, 960) + b"local-image"
        )
        vision_data = json.dumps(
            [{"text": "Ana", "confidence": 0.99, "x": 1.1, "y": 0.2, "width": 0.1, "height": 0.1}]
        ).encode()
        with (
            patch.object(self.android, "_adb", return_value="/opt/homebrew/bin/adb"),
            patch("assistente.android.shutil.which", return_value="/usr/bin/swift"),
            patch(
                "assistente.android.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess([], 0, screenshot, b""),
                    subprocess.CompletedProcess([], 0, vision_data, b""),
                ],
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "coordenadas OCR inválidas"):
                self.android._ocr_screen("127.0.0.1:5685")

    def test_ocr_context_requires_unique_high_confidence_header(self):
        screen = OCRScreen(
            540,
            960,
            (
                OCRRegion("Ana", 0.99, 0.3, 0.93, 0.2, 0.03),
                OCRRegion("Olá", 0.95, 0.2, 0.7, 0.2, 0.03),
            ),
        )
        self.assertEqual(AndroidEmulator._ocr_context(screen, "Ana"), "Olá")

        ambiguous = OCRScreen(
            540,
            960,
            screen.regions + (OCRRegion("Ana", 0.99, 0.6, 0.93, 0.2, 0.03),),
        )
        with self.assertRaisesRegex(RuntimeError, "único destinatário"):
            AndroidEmulator._ocr_context(ambiguous, "Ana")

    def test_ocr_matching_strips_only_leading_bullet_and_keeps_confidence_gate(self):
        screen = OCRScreen(
            540,
            960,
            (
                OCRRegion("• Atv_Sigilo", 0.99, 0.2, 0.7, 0.2, 0.03),
                OCRRegion("Atv_Sigilo", 0.91, 0.2, 0.6, 0.2, 0.03),
            ),
        )

        self.assertEqual(
            AndroidEmulator._matching_ocr_regions(screen, "Atv_Sigilo"),
            [screen.regions[0]],
        )

        duplicate = OCRScreen(
            540,
            960,
            screen.regions + (OCRRegion("Atv_Sigilo", 0.99, 0.6, 0.7, 0.2, 0.03),),
        )
        self.assertEqual(
            len(AndroidEmulator._matching_ocr_regions(duplicate, "Atv_Sigilo")),
            2,
        )

    def test_prepare_reply_uses_ocr_to_bind_header_context_and_approval_mode(self):
        screen = OCRScreen(
            540,
            960,
            (
                OCRRegion("Ana", 0.99, 0.3, 0.93, 0.2, 0.03),
                OCRRegion("Oi, tudo bem?", 0.98, 0.2, 0.5, 0.3, 0.03),
            ),
        )
        field = AndroidNode(
            "",
            "",
            "com.grindrapp.android:id/chat_bottom_input",
            "android.widget.EditText",
            (40, 866, 476, 886),
            True,
            True,
            "",
        )
        send = AndroidNode(
            "",
            "",
            "com.grindrapp.android:id/chat_edit_text_right_drawable",
            "android.widget.ImageView",
            (480, 856, 520, 896),
            True,
            True,
            "",
        )
        with (
            patch("assistente.android.shutil.which", return_value="/usr/bin/swift"),
            patch.object(self.android, "_validate_device"),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_hierarchy", return_value=[field, send]),
            patch.object(self.android, "_ocr_screen", return_value=screen),
        ):
            preview = self.android.prepare_reply("emulator-5554", "Ana", "Resposta")

        self.assertEqual(preview["recipient"], "Ana")
        self.assertEqual(preview["context"], "Oi, tudo bem?")
        self.assertEqual(preview["context_mode"], "vision")

    def test_ocr_reply_preview_rejects_low_confidence_or_missing_context(self):
        screen = OCRScreen(
            540,
            960,
            (
                OCRRegion("Ana", 0.7, 0.3, 0.93, 0.2, 0.03),
                OCRRegion("Oi", 0.98, 0.2, 0.5, 0.3, 0.03),
            ),
        )
        field = AndroidNode(
            "", "", "", "android.widget.EditText", (40, 866, 476, 886), True, True, ""
        )
        send = AndroidNode(
            "",
            "",
            "com.grindrapp.android:id/chat_edit_text_right_drawable",
            "android.widget.ImageView",
            (480, 856, 520, 896),
            True,
            True,
            "",
        )
        with (
            patch("assistente.android.shutil.which", return_value="/usr/bin/swift"),
            patch.object(self.android, "_validate_device"),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_hierarchy", return_value=[field, send]),
            patch.object(self.android, "_ocr_screen", return_value=screen),
        ):
            with self.assertRaisesRegex(RuntimeError, "único destinatário"):
                self.android.prepare_reply("emulator-5554", "Ana", "Resposta")

    def test_prepare_reply_falls_back_to_verified_incoming_accessibility_message(self):
        nodes = [
            AndroidNode(
                "Atv_Sigilo",
                "Chat: Atv_Sigilo",
                "com.grindrapp.android:id/toolbar_title",
                "android.widget.TextView",
                (96, 34, 149, 52),
                False,
                True,
                "",
            ),
            AndroidNode(
                "Oi",
                "",
                "",
                "android.widget.TextView",
                (32, 775, 47, 795),
                False,
                True,
                "",
            ),
            AndroidNode(
                "Toque duas vezes para curtir",
                "",
                "",
                "android.widget.TextView",
                (60, 815, 215, 830),
                False,
                True,
                "",
            ),
            AndroidNode(
                "",
                "",
                "com.grindrapp.android:id/chat_bottom_input",
                "android.widget.EditText",
                (40, 866, 476, 886),
                True,
                True,
                "",
            ),
            AndroidNode(
                "",
                "",
                "com.grindrapp.android:id/chat_edit_text_right_drawable",
                "android.widget.ImageView",
                (480, 856, 520, 896),
                True,
                True,
                "",
            ),
            AndroidNode(
                "",
                "",
                "",
                "android.view.View",
                (0, 0, 540, 960),
                False,
                True,
                "",
            ),
        ]
        screen = OCRScreen(
            540,
            960,
            (OCRRegion("Atv_Sigilo", 1.0, 0.17, 0.94, 0.11, 0.02),),
        )
        with (
            patch("assistente.android.shutil.which", return_value="/usr/bin/swift"),
            patch.object(self.android, "_validate_device"),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_hierarchy", return_value=nodes),
            patch.object(self.android, "_ocr_screen", return_value=screen),
        ):
            preview = self.android.prepare_reply("127.0.0.1:5685", "Atv_Sigilo", "Oi! Tudo bem?")

        self.assertEqual(preview["recipient"], "Atv_Sigilo")
        self.assertEqual(
            preview["context"],
            "Atv_Sigilo\nMensagem recebida mais recente: Oi",
        )
        self.assertEqual(preview["context_mode"], "accessibility_incoming")

    def test_incoming_accessibility_context_requires_verified_title_and_message(self):
        title = AndroidNode(
            "Ana",
            "Chat: Ana",
            "com.grindrapp.android:id/toolbar_title",
            "android.widget.TextView",
            (96, 34, 149, 52),
            False,
            True,
            "",
        )
        field = AndroidNode(
            "",
            "",
            "com.grindrapp.android:id/chat_bottom_input",
            "android.widget.EditText",
            (40, 866, 476, 886),
            True,
            True,
            "",
        )
        root = AndroidNode("", "", "", "android.view.View", (0, 0, 540, 960), False, True, "")

        with self.assertRaisesRegex(RuntimeError, "mensagem recebida visível"):
            self.android._incoming_accessibility_context([title, field, root], "Ana")
        with self.assertRaisesRegex(RuntimeError, "cabeçalho"):
            self.android._incoming_accessibility_context([field, root], "Ana")

    def test_incoming_accessibility_reply_aborts_if_latest_received_message_changed(self):
        title = AndroidNode(
            "Ana",
            "Chat: Ana",
            "com.grindrapp.android:id/toolbar_title",
            "android.widget.TextView",
            (96, 34, 149, 52),
            False,
            True,
            "",
        )
        changed_message = AndroidNode(
            "Oi de novo",
            "",
            "",
            "android.widget.TextView",
            (32, 775, 130, 795),
            False,
            True,
            "",
        )
        field = AndroidNode(
            "",
            "",
            "com.grindrapp.android:id/chat_bottom_input",
            "android.widget.EditText",
            (40, 866, 476, 886),
            True,
            True,
            "",
        )
        button = AndroidNode(
            "",
            "",
            "com.grindrapp.android:id/chat_edit_text_right_drawable",
            "android.widget.ImageView",
            (480, 856, 520, 896),
            True,
            True,
            "",
        )
        root = AndroidNode("", "", "", "android.view.View", (0, 0, 540, 960), False, True, "")
        with (
            patch.object(self.android, "_validate_device"),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(
                self.android,
                "_hierarchy",
                return_value=[title, changed_message, field, button, root],
            ),
            patch.object(self.android, "_tap") as tap,
            patch.object(self.android, "_run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "mudou após a confirmação"):
                self.android.send_approved_reply(
                    "127.0.0.1:5685",
                    "Ana",
                    "Resposta aprovada",
                    "Ana\nMensagem recebida mais recente: Oi",
                    "accessibility_incoming",
                )

        tap.assert_not_called()
        run.assert_not_called()

    def test_reply_accepts_known_grindr_send_button_transition_after_draft(self):
        text = "Oi! Tudo bem?"
        title = AndroidNode(
            "Atv_Sigilo",
            "Chat: Atv_Sigilo",
            "com.grindrapp.android:id/toolbar_title",
            "android.widget.TextView",
            (96, 34, 149, 52),
            False,
            True,
            "",
        )
        incoming = AndroidNode(
            "Oi",
            "",
            "",
            "android.widget.TextView",
            (32, 775, 47, 795),
            False,
            True,
            "",
        )
        field = AndroidNode(
            "",
            "",
            "com.grindrapp.android:id/chat_bottom_input",
            "android.widget.EditText",
            (40, 866, 476, 886),
            True,
            True,
            "",
        )
        old_button = AndroidNode(
            "",
            "",
            "com.grindrapp.android:id/chat_edit_text_right_drawable",
            "android.widget.ImageView",
            (480, 856, 520, 896),
            True,
            True,
            "",
        )
        new_button = AndroidNode(
            "",
            "",
            "com.grindrapp.android:id/chat_edit_text_send_btn",
            "android.widget.FrameLayout",
            (480, 860, 520, 900),
            True,
            True,
            "",
        )
        root = AndroidNode("", "", "", "android.view.View", (0, 0, 540, 960), False, True, "")
        draft_field = AndroidNode(
            text,
            "",
            field.resource_id,
            field.class_name,
            field.bounds,
            True,
            True,
            text,
        )
        final_message = AndroidNode(
            text,
            "",
            "",
            "android.widget.TextView",
            (360, 700, 500, 730),
            False,
            True,
            "",
        )
        initial = [title, incoming, field, old_button, root]
        draft = [title, incoming, draft_field, new_button, root]
        final = [title, incoming, final_message, field, new_button, root]
        with (
            patch.object(self.android, "_validate_device"),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_hierarchy", side_effect=[initial, draft, final]),
            patch.object(self.android, "_tap") as tap,
            patch.object(
                self.android,
                "_run",
                side_effect=["Physical size: 540x960", ""],
            ) as run,
        ):
            result = self.android.send_approved_reply(
                "127.0.0.1:5685",
                "Atv_Sigilo",
                text,
                "Atv_Sigilo\nMensagem recebida mais recente: Oi",
                "accessibility_incoming",
            )

        self.assertIn("Resposta enviada", result)
        self.assertEqual(tap.call_count, 2)
        self.assertEqual(tap.call_args_list[0].args, ("127.0.0.1:5685", field))
        self.assertEqual(tap.call_args_list[1].args, ("127.0.0.1:5685", new_button))
        self.assertEqual(run.call_args.args[0][4], "text")

    def test_vision_reply_aborts_before_drafting_if_approval_context_changed(self):
        changed = OCRScreen(
            540,
            960,
            (
                OCRRegion("Outra pessoa", 0.99, 0.3, 0.93, 0.2, 0.03),
                OCRRegion("Oi", 0.98, 0.2, 0.5, 0.3, 0.03),
            ),
        )
        with (
            patch.object(self.android, "_validate_device"),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_hierarchy", return_value=[]),
            patch.object(self.android, "_ocr_screen", return_value=changed),
            patch.object(self.android, "_tap") as tap,
            patch.object(self.android, "_run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "único destinatário"):
                self.android.send_approved_reply(
                    "emulator-5554",
                    "Ana",
                    "Resposta",
                    "Oi",
                    "vision",
                )

        tap.assert_not_called()
        run.assert_not_called()

    def test_vision_reply_revalidates_context_and_exact_draft_before_send_tap(self):
        text = "Resposta exata"
        initial_screen = OCRScreen(
            540,
            960,
            (
                OCRRegion("Ana", 0.99, 0.3, 0.93, 0.2, 0.03),
                OCRRegion("Oi", 0.98, 0.2, 0.5, 0.3, 0.03),
            ),
        )
        final_screen = OCRScreen(
            540,
            960,
            (
                OCRRegion("Ana", 0.99, 0.3, 0.93, 0.2, 0.03),
                OCRRegion("Oi", 0.98, 0.2, 0.56, 0.3, 0.03),
                OCRRegion(text, 0.99, 0.2, 0.5, 0.3, 0.03),
            ),
        )
        field = AndroidNode(
            "",
            "",
            "com.grindrapp.android:id/chat_bottom_input",
            "android.widget.EditText",
            (40, 866, 476, 886),
            True,
            True,
            "",
        )
        draft_field = AndroidNode(
            text,
            "",
            "com.grindrapp.android:id/chat_bottom_input",
            "android.widget.EditText",
            (40, 866, 476, 886),
            True,
            True,
            text,
        )
        send = AndroidNode(
            "",
            "",
            "com.grindrapp.android:id/chat_edit_text_right_drawable",
            "android.widget.ImageView",
            (480, 856, 520, 896),
            True,
            True,
            "",
        )
        with (
            patch.object(self.android, "_validate_device"),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(
                self.android, "_hierarchy", side_effect=[[field, send], [draft_field, send]]
            ),
            patch.object(
                self.android,
                "_ocr_screen",
                side_effect=[initial_screen, initial_screen, final_screen],
            ),
            patch.object(self.android, "_tap") as tap,
            patch.object(self.android, "_run", return_value="") as run,
        ):
            result = self.android.send_approved_reply(
                "emulator-5554",
                "Ana",
                text,
                "Oi",
                "vision",
            )

        self.assertIn("Resposta enviada", result)
        self.assertEqual(tap.call_count, 2)
        self.assertEqual(tap.call_args_list[0].args, ("emulator-5554", field))
        self.assertEqual(tap.call_args_list[1].args, ("emulator-5554", send))
        self.assertEqual(run.call_args.args[0][4], "text")

    def test_ocr_tap_converts_vision_coordinates_and_checks_screen_geometry(self):
        screen = OCRScreen(
            540,
            960,
            (OCRRegion("Ana", 0.99, 0.1, 0.5, 0.2, 0.05),),
        )
        root = AndroidNode("", "", "", "android.view.View", (0, 0, 540, 960), False, True, "")
        with (
            patch.object(self.android, "_hierarchy", return_value=[root]),
            patch.object(
                self.android,
                "_run",
                side_effect=["Physical size: 540x960", ""],
            ) as run,
        ):
            self.android._tap_ocr_region("127.0.0.1:5685", screen, screen.regions[0])

        self.assertEqual(
            run.call_args_list[-1].args[0],
            ["-s", "127.0.0.1:5685", "shell", "input", "tap", "108", "456"],
        )

    def test_ocr_tap_uses_android_logical_size_not_accessibility_extent(self):
        screen = OCRScreen(
            540,
            960,
            (OCRRegion("Atv_Sigilo", 1.0, 0.2, 0.5, 0.2, 0.05),),
        )
        partial_hierarchy = AndroidNode(
            "",
            "",
            "",
            "android.view.View",
            (0, 0, 540, 908),
            False,
            True,
            "",
        )
        with (
            patch.object(self.android, "_hierarchy", return_value=[partial_hierarchy]),
            patch.object(
                self.android,
                "_run",
                side_effect=["Physical size: 1080x1920\nOverride size: 540x960", ""],
            ) as run,
        ):
            self.android._tap_ocr_region("127.0.0.1:5685", screen, screen.regions[0])

        self.assertEqual(
            run.call_args_list[-1].args[0],
            ["-s", "127.0.0.1:5685", "shell", "input", "tap", "162", "456"],
        )

    def test_ocr_conversation_open_requires_unique_target_and_verified_header(self):
        list_screen = OCRScreen(
            540,
            960,
            (OCRRegion("Ana", 0.99, 0.1, 0.5, 0.2, 0.05),),
        )
        header = AndroidNode(
            "Ana",
            "Chat: Ana",
            "com.grindrapp.android:id/toolbar_title",
            "android.widget.TextView",
            (96, 34, 149, 52),
            False,
            True,
            "",
        )
        root = AndroidNode("", "", "", "android.view.View", (0, 0, 540, 960), False, True, "")
        with (
            patch("assistente.android.shutil.which", return_value="/usr/bin/swift"),
            patch.object(self.android, "_validate_device"),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_hierarchy", side_effect=[[root], [root], [header]]),
            patch.object(self.android, "_ocr_screen", return_value=list_screen),
            patch.object(
                self.android,
                "_run",
                side_effect=["Physical size: 540x960", ""],
            ) as run,
        ):
            result = self.android.open_conversation("127.0.0.1:5685", "Ana")

        self.assertIn("Conversa selecionada: Ana", result)
        self.assertEqual(
            run.call_args.args[0],
            ["-s", "127.0.0.1:5685", "shell", "input", "tap", "108", "456"],
        )

    def test_open_conversation_verifies_fresh_unique_accessibility_header(self):
        row = AndroidNode(
            "Ana",
            "",
            "",
            "android.widget.TextView",
            (100, 400, 180, 440),
            True,
            True,
            "",
        )
        header = AndroidNode(
            "Ana",
            "Chat: Ana",
            "com.grindrapp.android:id/toolbar_title",
            "android.widget.TextView",
            (96, 34, 149, 52),
            False,
            True,
            "",
        )
        with (
            patch.object(self.android, "_validate_device"),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_hierarchy", side_effect=[[row], [header]]) as hierarchy,
            patch.object(self.android, "_tap") as tap,
            patch("assistente.android.shutil.which", return_value=None),
        ):
            result = self.android.open_conversation("emulator-5554", "Ana")

        self.assertIn("Conversa selecionada: Ana", result)
        self.assertEqual(hierarchy.call_count, 2)
        tap.assert_called_once_with("emulator-5554", row)

    def test_open_conversation_rejects_missing_or_duplicate_accessibility_header(self):
        row = AndroidNode(
            "Ana",
            "",
            "",
            "android.widget.TextView",
            (100, 400, 180, 440),
            True,
            True,
            "",
        )
        header = AndroidNode(
            "Ana",
            "Chat: Ana",
            "com.grindrapp.android:id/toolbar_title",
            "android.widget.TextView",
            (96, 34, 149, 52),
            False,
            True,
            "",
        )
        for headers in ([], [header, header]):
            with (
                patch.object(self.android, "_validate_device"),
                patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
                patch.object(self.android, "_hierarchy", side_effect=[[row], headers]),
                patch.object(self.android, "_tap") as tap,
                patch("assistente.android.shutil.which", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, "único cabeçalho"):
                    self.android.open_conversation("emulator-5554", "Ana")
            tap.assert_called_once_with("emulator-5554", row)

    def test_ocr_conversation_open_rejects_ambiguous_target_without_tapping(self):
        screen = OCRScreen(
            540,
            960,
            (
                OCRRegion("Ana", 0.99, 0.1, 0.5, 0.2, 0.05),
                OCRRegion("Ana", 0.98, 0.6, 0.5, 0.2, 0.05),
            ),
        )
        with (
            patch("assistente.android.shutil.which", return_value="/usr/bin/swift"),
            patch.object(self.android, "_validate_device"),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_hierarchy", return_value=[]),
            patch.object(self.android, "_ocr_screen", return_value=screen),
            patch.object(self.android, "_run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "rótulo único"):
                self.android.open_conversation("127.0.0.1:5685", "Ana")

        run.assert_not_called()

    def test_reply_controls_accept_only_the_known_composer_send_control(self):
        field = AndroidNode(
            "", "", "", "android.widget.EditText", (40, 866, 476, 886), True, True, ""
        )
        send = AndroidNode(
            "",
            "",
            "com.grindrapp.android:id/chat_edit_text_right_drawable",
            "android.widget.ImageView",
            (480, 856, 520, 896),
            True,
            True,
            "",
        )
        self.assertEqual(AndroidEmulator._reply_controls([field, send]), (field, send))

        wrong_control = AndroidNode(
            "",
            "",
            "com.grindrapp.android:id/chat_bottom_button_photo",
            "android.widget.ImageButton",
            (480, 856, 520, 896),
            True,
            True,
            "",
        )
        with self.assertRaisesRegex(RuntimeError, "campo de resposta"):
            AndroidEmulator._reply_controls([field, wrong_control])

    def test_observe_open_chat_reads_only_visible_accessible_text_in_current_chat(self):
        nodes = [
            AndroidNode(
                "Ana",
                "Chat: Ana",
                f"{GRINDR_PACKAGE}:id/toolbar_title",
                "android.widget.TextView",
                (96, 34, 149, 52),
                False,
                True,
                "",
            ),
            AndroidNode(
                "Sem foto",
                "",
                "",
                "android.widget.TextView",
                (160, 80, 260, 105),
                False,
                True,
                "",
            ),
            AndroidNode(
                "",
                "",
                "",
                "android.widget.EditText",
                (40, 866, 476, 886),
                True,
                True,
                "",
            ),
            AndroidNode(
                "Oi, tudo bem?",
                "",
                "",
                "android.widget.TextView",
                (32, 775, 150, 795),
                False,
                True,
                "",
            ),
            AndroidNode(
                "Tudo certo, e você?",
                "",
                "",
                "android.widget.TextView",
                (350, 672, 508, 712),
                False,
                True,
                "",
            ),
            AndroidNode(
                "mensagem central ambígua",
                "",
                "",
                "android.widget.TextView",
                (200, 500, 330, 520),
                False,
                True,
                "",
            ),
            AndroidNode(
                "14:13",
                "",
                "",
                "android.widget.TextView",
                (20, 815, 52, 830),
                False,
                True,
                "",
            ),
            AndroidNode(
                '"Entregue"',
                "",
                "",
                "android.widget.TextView",
                (374, 760, 419, 772),
                False,
                True,
                "",
            ),
            AndroidNode(
                "Cancelar envio",
                "",
                "",
                "android.widget.TextView",
                (465, 760, 525, 772),
                False,
                True,
                "",
            ),
            AndroidNode(
                "Confirmações de Leitura Grátis",
                "",
                "",
                "android.widget.TextView",
                (57, 787, 206, 800),
                False,
                True,
                "",
            ),
            AndroidNode(
                "Assista a um vídeo para desbloquear por 1 hora",
                "",
                "",
                "android.widget.TextView",
                (57, 802, 253, 816),
                False,
                True,
                "",
            ),
        ]
        with (
            patch.object(
                self.android,
                "active_grindr_instance",
                return_value={"serial": "127.0.0.1:5685"},
            ) as active_instance,
            patch.object(self.android, "_hierarchy", return_value=nodes),
        ):
            result = self.android.observe_open_chat("127.0.0.1:5685")

        active_instance.assert_called_once_with("127.0.0.1:5685")
        self.assertEqual(result["conversation"], "Ana")
        self.assertTrue(result["conversation_identified"])
        self.assertEqual(result["profile_photo_status"], "sem foto")
        self.assertEqual(
            result["messages"],
            [
                {"direction": "sent", "text": "Tudo certo, e você?"},
                {"direction": "received", "text": "Oi, tudo bem?"},
            ],
        )

    def test_observe_open_chat_abstains_without_unique_open_chat_header(self):
        field = AndroidNode(
            "", "", "", "android.widget.EditText", (40, 866, 476, 886), True, True, ""
        )
        with (
            patch.object(
                self.android,
                "active_grindr_instance",
                return_value={"serial": "127.0.0.1:5685"},
            ),
            patch.object(self.android, "_hierarchy", return_value=[field]),
        ):
            self.assertIsNone(self.android.observe_open_chat())

    def test_observe_open_chat_preserves_full_visible_message_text(self):
        long_message = "Mensagem visível " * 25
        nodes = [
            AndroidNode(
                "Ana",
                "Chat: Ana",
                f"{GRINDR_PACKAGE}:id/toolbar_title",
                "android.widget.TextView",
                (96, 34, 149, 52),
                False,
                True,
                "",
            ),
            AndroidNode(
                "",
                "",
                "",
                "android.widget.EditText",
                (40, 866, 476, 886),
                True,
                True,
                "",
            ),
            AndroidNode(
                long_message,
                "",
                "",
                "android.widget.TextView",
                (32, 775, 150, 795),
                False,
                True,
                "",
            ),
        ]
        with (
            patch.object(
                self.android,
                "active_grindr_instance",
                return_value={"serial": "127.0.0.1:5685"},
            ),
            patch.object(self.android, "_hierarchy", return_value=nodes),
        ):
            result = self.android.observe_open_chat("127.0.0.1:5685")

        self.assertEqual(result["messages"], [{"direction": "received", "text": long_message}])
        self.assertEqual(result["profile_photo_status"], "não identificado")

    def test_observe_inbox_previews_reads_only_visible_rows_without_opening_them(self):
        nodes = [
            AndroidNode(
                "Caixa de entrada",
                "",
                "",
                "android.widget.TextView",
                (16, 32, 158, 80),
                False,
                True,
                "",
            ),
            AndroidNode(
                "",
                "",
                "",
                "android.view.View",
                (0, 144, 540, 240),
                True,
                True,
                "",
            ),
            AndroidNode(
                "Renato",
                "",
                "",
                "android.widget.TextView",
                (112, 158, 209, 178),
                False,
                True,
                "",
            ),
            AndroidNode(
                "Tudo bem?",
                "",
                "",
                "android.widget.TextView",
                (112, 184, 239, 204),
                False,
                True,
                "",
            ),
            AndroidNode(
                "hoje",
                "",
                "",
                "android.widget.TextView",
                (432, 158, 524, 178),
                False,
                True,
                "",
            ),
            AndroidNode(
                "",
                "",
                "",
                "android.view.View",
                (0, 910, 540, 960),
                True,
                True,
                "",
            ),
        ]
        with (
            patch.object(
                self.android,
                "active_grindr_instance",
                return_value={"serial": "127.0.0.1:5685"},
            ),
            patch.object(self.android, "_hierarchy", return_value=nodes),
            patch.object(self.android, "open_conversation") as open_conversation,
        ):
            previews = self.android.observe_inbox_previews("127.0.0.1:5685")

        self.assertEqual(len(previews), 1)
        self.assertEqual(previews[0]["profile"], "Renato")
        self.assertEqual(previews[0]["text"], "Tudo bem?")
        self.assertEqual(len(previews[0]["id"]), 64)
        open_conversation.assert_not_called()

    def test_observe_inbox_previews_abstains_outside_the_inbox(self):
        nodes = [
            AndroidNode(
                "Mensagens",
                "",
                "",
                "android.widget.TextView",
                (192, 927, 419, 943),
                False,
                True,
                "",
            ),
            AndroidNode(
                "",
                "",
                "",
                "android.view.View",
                (0, 276, 540, 860),
                True,
                True,
                "",
            ),
            AndroidNode(
                "",
                "",
                "",
                "android.widget.EditText",
                (40, 866, 476, 886),
                True,
                True,
                "",
            ),
        ]
        with (
            patch.object(
                self.android,
                "active_grindr_instance",
                return_value={"serial": "127.0.0.1:5685"},
            ),
            patch.object(self.android, "_hierarchy", return_value=nodes),
        ):
            self.assertIsNone(self.android.observe_inbox_previews("127.0.0.1:5685"))

    def test_observe_open_chat_reads_current_toolbar_layout_without_guessing_profile_name(self):
        nodes = [
            AndroidNode(
                "",
                "",
                f"{GRINDR_PACKAGE}:id/toolbar_header",
                "android.view.ViewGroup",
                (56, 34, 226, 70),
                True,
                True,
                "",
            ),
            AndroidNode(
                "Distância no chat: 333 metros de distância",
                "Distância no chat: 333 metros de distância",
                f"{GRINDR_PACKAGE}:id/toolbar_distance",
                "android.widget.TextView",
                (96, 52, 226, 70),
                False,
                True,
                "333 metros de distância",
            ),
            AndroidNode(
                "",
                "Bate-papo online: true",
                f"{GRINDR_PACKAGE}:id/toolbar_online_indicator",
                "android.widget.ImageView",
                (81, 61, 89, 69),
                False,
                True,
                "",
            ),
            AndroidNode(
                "",
                "",
                "",
                "android.widget.EditText",
                (40, 866, 476, 886),
                True,
                True,
                "",
            ),
            AndroidNode(
                "",
                "",
                f"{GRINDR_PACKAGE}:id/chat_edit_text_right_drawable",
                "android.widget.ImageView",
                (480, 856, 520, 896),
                True,
                True,
                "",
            ),
            AndroidNode(
                "eae blz?",
                "",
                "",
                "android.widget.TextView",
                (32, 775, 94, 795),
                False,
                True,
                "",
            ),
            AndroidNode(
                "16:51",
                "",
                "",
                "android.widget.TextView",
                (20, 815, 52, 830),
                False,
                True,
                "",
            ),
        ]
        with (
            patch.object(
                self.android,
                "active_grindr_instance",
                return_value={"serial": "127.0.0.1:5685"},
            ),
            patch.object(self.android, "_hierarchy", return_value=nodes),
        ):
            result = self.android.observe_open_chat("127.0.0.1:5685")

        self.assertEqual(result["conversation"], "não identificado")
        self.assertFalse(result["conversation_identified"])
        self.assertEqual(result["profile_photo_status"], "não identificado")
        self.assertEqual(
            result["messages"],
            [{"direction": "received", "text": "eae blz?"}],
        )

    def test_grindr_instance_statuses_reports_connected_emulators_and_foreground_state(self):
        emulator_properties = {
            "ro.product.manufacturer": "BlueStacks",
            "ro.product.model": "Android",
            "ro.product.brand": "BlueStacks",
            "ro.product.device": "bluestacks",
            "ro.boot.qemu": "1",
        }
        with (
            patch.object(
                self.android,
                "_device_rows",
                return_value=[
                    ("127.0.0.1:5685", "product:bluestacks"),
                    ("phone-serial", "product:pixel"),
                ],
            ),
            patch.object(self.android, "_properties", return_value=emulator_properties),
            patch.object(
                self.android,
                "_is_emulator",
                side_effect=[True, False],
            ),
            patch.object(
                self.android,
                "_foreground",
                return_value=GRINDR_PACKAGE,
            ) as foreground,
        ):
            statuses = self.android.grindr_instance_statuses()

        self.assertEqual(
            statuses,
            [
                {
                    "serial": "127.0.0.1:5685",
                    "instance_name": "Ipanema (Tiramisu64_13)",
                    "description": "BlueStacks Android bluestacks product:bluestacks",
                    "grindr_foreground": True,
                }
            ],
        )
        foreground.assert_called_once_with("127.0.0.1:5685")

    def test_grindr_instance_statuses_isolates_foreground_detection_errors(self):
        emulator_properties = {
            "ro.product.manufacturer": "BlueStacks",
            "ro.product.model": "Android",
            "ro.product.brand": "BlueStacks",
            "ro.product.device": "bluestacks",
            "ro.boot.qemu": "1",
        }
        error = "Não foi possível identificar o aplicativo Android em primeiro plano."
        with (
            patch.object(
                self.android,
                "_device_rows",
                return_value=[
                    ("127.0.0.1:5645", "product:bluestacks"),
                    ("127.0.0.1:5685", "product:bluestacks"),
                ],
            ),
            patch.object(self.android, "_properties", return_value=emulator_properties),
            patch.object(self.android, "_is_emulator", return_value=True),
            patch.object(
                self.android,
                "_foreground",
                side_effect=[RuntimeError(error), GRINDR_PACKAGE],
            ),
        ):
            statuses = self.android.grindr_instance_statuses()

        self.assertEqual(
            [status["grindr_foreground"] for status in statuses],
            [None, True],
        )
        self.assertEqual(statuses[0]["foreground_error"], error)
        self.assertNotIn("foreground_error", statuses[1])

    def test_bluestacks_instance_names_use_known_labels_and_safe_port_fallback(self):
        self.assertEqual(
            self.android.instance_name("127.0.0.1:5645"),
            "Tiramisu64_9",
        )
        self.assertEqual(
            self.android.instance_name("127.0.0.1:5655"),
            "Tiramisu64_10",
        )
        self.assertEqual(
            self.android.instance_name("127.0.0.1:5685"),
            "Ipanema (Tiramisu64_13)",
        )
        self.assertEqual(
            self.android.instance_name("127.0.0.1:5665"),
            "BlueStacks (porta 5665)",
        )
        self.assertEqual(self.android.instance_name("emulator-5554"), "Emulador (emulator-5554)")

    def test_lists_online_android_emulators_without_connecting(self):
        with (
            patch.object(
                self.android,
                "_run",
                side_effect=[
                    "List of devices attached\n127.0.0.1:5555 device product:bluestacks model:BlueStacks\n",
                    "BlueStacks\n",
                    "Android\n",
                    "BlueStacks\n",
                    "bluestacks\n",
                    "1\n",
                ],
            ) as run,
        ):
            devices = json.loads(self.android.list_devices())["devices"]

        self.assertEqual(devices[0]["serial"], "127.0.0.1:5555")
        self.assertEqual(devices[0]["instance_name"], "BlueStacks (porta 5555)")
        self.assertTrue(devices[0]["likely_emulator"])
        self.assertTrue(devices[0]["bluestacks_confirmed"])
        self.assertEqual(run.call_count, 6)

    def test_no_online_adb_devices_is_an_explicit_error(self):
        with patch.object(
            self.android,
            "_run",
            return_value="List of devices attached\n",
        ):
            with self.assertRaisesRegex(RuntimeError, "Nenhum dispositivo Android"):
                self.android.list_devices()

    def test_active_grindr_instance_selects_only_a_unique_emulator_in_foreground(self):
        properties = {
            "ro.product.manufacturer": "BlueStacks",
            "ro.product.model": "Android",
            "ro.product.brand": "BlueStacks",
            "ro.product.device": "bluestacks",
            "ro.boot.qemu": "1",
        }
        with (
            patch.object(self.android, "_device_rows", return_value=[("127.0.0.1:5685", "")]),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_properties", return_value=properties),
        ):
            instance = self.android.active_grindr_instance()

        self.assertEqual(instance["serial"], "127.0.0.1:5685")
        self.assertEqual(instance["bluestacks_confirmed"], "true")

    def test_active_grindr_instance_fails_closed_on_multiple_matches_or_unknown_state(self):
        properties = {
            "ro.product.manufacturer": "BlueStacks",
            "ro.product.model": "Android",
            "ro.product.brand": "BlueStacks",
            "ro.product.device": "bluestacks",
            "ro.boot.qemu": "1",
        }
        with (
            patch.object(
                self.android,
                "_device_rows",
                return_value=[("127.0.0.1:5685", ""), ("127.0.0.1:5686", "")],
            ),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_properties", return_value=properties),
        ):
            with self.assertRaisesRegex(RuntimeError, "encontrei 2"):
                self.android.active_grindr_instance()

        with (
            patch.object(
                self.android,
                "_device_rows",
                return_value=[("127.0.0.1:5685", ""), ("127.0.0.1:5686", "")],
            ),
            patch.object(
                self.android,
                "_foreground",
                side_effect=[RuntimeError("estado indisponível"), GRINDR_PACKAGE],
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "estado indisponível"):
                self.android.active_grindr_instance()

    def test_active_grindr_instance_accepts_one_explicit_device_among_multiple_instances(self):
        properties = {
            "ro.product.manufacturer": "BlueStacks",
            "ro.product.model": "Android",
            "ro.product.brand": "BlueStacks",
            "ro.product.device": "bluestacks",
            "ro.boot.qemu": "1",
        }
        with (
            patch.object(
                self.android,
                "_device_rows",
                return_value=[
                    ("127.0.0.1:5645", ""),
                    ("127.0.0.1:5655", ""),
                    ("127.0.0.1:5685", ""),
                ],
            ),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE) as foreground,
            patch.object(self.android, "_properties", return_value=properties),
        ):
            instance = self.android.active_grindr_instance("127.0.0.1:5655")

        self.assertEqual(instance["serial"], "127.0.0.1:5655")
        self.assertEqual(instance["bluestacks_confirmed"], "true")
        foreground.assert_called_once_with("127.0.0.1:5655")

    def test_list_inbox_returns_private_ocr_evidence_without_guessing_senders(self):
        properties = {
            "ro.product.manufacturer": "BlueStacks",
            "ro.product.model": "Android",
            "ro.product.brand": "BlueStacks",
            "ro.product.device": "bluestacks",
            "ro.boot.qemu": "1",
        }
        screen = OCRScreen(
            540,
            960,
            (
                OCRRegion("Mensagens", 0.99, 0.1, 0.94, 0.4, 0.03),
                OCRRegion("Ana", 0.98, 0.1, 0.8, 0.2, 0.03),
                OCRRegion("Oi, tudo bem?", 0.97, 0.1, 0.74, 0.4, 0.03),
            ),
        )
        with (
            patch.object(
                self.android,
                "_device_rows",
                return_value=[("127.0.0.1:5685", "product:bluestacks")],
            ),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_properties", return_value=properties),
            patch.object(self.android, "_hierarchy", return_value=[]),
            patch.object(self.android, "_ocr_screen", return_value=screen),
        ):
            result = json.loads(self.android.list_inbox())

        self.assertEqual(result["serial"], "127.0.0.1:5685")
        self.assertEqual(result["screen"], "grindr_inbox")
        self.assertEqual(result["ocr_observations"][1]["text"], "Ana")
        self.assertIn("direção como ambígua", result["instruction"])

    def test_list_inbox_can_read_a_user_selected_instance_when_multiple_are_active(self):
        properties = {
            "ro.product.manufacturer": "BlueStacks",
            "ro.product.model": "Android",
            "ro.product.brand": "BlueStacks",
            "ro.product.device": "bluestacks",
            "ro.boot.qemu": "1",
        }
        screen = OCRScreen(
            540,
            960,
            (
                OCRRegion("Caixa de entrada", 0.99, 0.1, 0.94, 0.4, 0.03),
                OCRRegion("Renato", 0.98, 0.1, 0.8, 0.2, 0.03),
            ),
        )
        with (
            patch.object(
                self.android,
                "_device_rows",
                return_value=[
                    ("127.0.0.1:5645", ""),
                    ("127.0.0.1:5655", ""),
                    ("127.0.0.1:5685", ""),
                ],
            ),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_properties", return_value=properties),
            patch.object(self.android, "_hierarchy", return_value=[]),
            patch.object(self.android, "_ocr_screen", return_value=screen),
        ):
            result = json.loads(self.android.list_inbox(serial="127.0.0.1:5655"))

        self.assertEqual(result["serial"], "127.0.0.1:5655")
        self.assertTrue(result["bluestacks_confirmed"])
        self.assertEqual(result["ocr_observations"][1]["text"], "Renato")

    def test_list_inbox_confirms_low_confidence_header_with_inbox_layout(self):
        screen = OCRScreen(
            540,
            960,
            (
                OCRRegion("Caixa de entrada", 0.5, 0.02, 0.94, 0.2, 0.03),
                OCRRegion("Álbuns", 1.0, 0.24, 0.94, 0.1, 0.03),
                OCRRegion("Não lidas", 1.0, 0.12, 0.82, 0.1, 0.03),
                OCRRegion("Distância", 1.0, 0.27, 0.82, 0.1, 0.03),
                OCRRegion("Renato", 0.98, 0.15, 0.64, 0.2, 0.03),
            ),
        )
        with (
            patch.object(
                self.android,
                "active_grindr_instance",
                return_value={
                    "serial": "127.0.0.1:5655",
                    "description": "BlueStacks",
                    "bluestacks_confirmed": "true",
                },
            ),
            patch.object(self.android, "_hierarchy", return_value=[]),
            patch.object(self.android, "_ocr_screen", return_value=screen),
            patch.object(self.android, "open_messages") as open_messages,
        ):
            result = json.loads(self.android.list_inbox())

        self.assertEqual(result["screen"], "grindr_inbox")
        self.assertIn(
            "Álbuns",
            [item["text"] for item in result["ocr_observations"]],
        )
        open_messages.assert_not_called()

    def test_analyze_screen_parses_only_visible_accessibility_nodes(self):
        hierarchy = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
            '<hierarchy><node text="Ana" content-desc="" resource-id="grindr:id/title" '
            'class="android.widget.TextView" clickable="false" enabled="true" '
            'bounds="[0,0][100,40]" /><node text="" content-desc="Send" '
            'resource-id="grindr:id/send" class="android.widget.Button" '
            'clickable="true" enabled="true" bounds="[100,200][150,250]" />'
            '<node text="private input" content-desc="" resource-id="grindr:id/password" '
            'class="android.widget.EditText" clickable="false" enabled="true" '
            'bounds="[0,250][100,300]" /></hierarchy>UI hierchary dumped to: /dev/tty'
        )
        with (
            patch.object(self.android, "_validate_device"),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_run", return_value=hierarchy),
            patch.object(
                self.android,
                "_ocr_screen",
                return_value=OCRScreen(540, 960, ()),
            ),
        ):
            result = json.loads(self.android.analyze_screen("emulator-5554"))

        self.assertTrue(result["is_grindr"])
        self.assertEqual(result["visible_elements"][0]["text"], "Ana")
        self.assertEqual(result["visible_elements"][1]["bounds"], [100, 200, 150, 250])
        self.assertEqual(result["visible_elements"][2]["text"], "[valor de campo oculto]")

    def test_reply_rejects_unknown_or_ambiguous_recipient_before_tapping(self):
        node = AndroidNode(
            text="Ana",
            description="",
            resource_id="",
            class_name="android.widget.TextView",
            bounds=(0, 0, 10, 10),
            clickable=False,
            enabled=True,
            input_text="Ana",
        )
        duplicate = [node, node]
        with (
            patch.object(self.android, "_validate_device"),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_hierarchy", return_value=duplicate),
        ):
            with self.assertRaisesRegex(RuntimeError, "exatamente uma vez"):
                self.android.prepare_reply("emulator-5554", "Ana", "Oi")

    def test_reply_refuses_to_overwrite_an_existing_draft(self):
        nodes = [
            AndroidNode(
                "Ana", "", "", "android.widget.TextView", (0, 0, 10, 10), False, True, "Ana"
            ),
            AndroidNode(
                "",
                "",
                "grindr:id/compose",
                "android.widget.EditText",
                (0, 10, 100, 30),
                False,
                True,
                "rascunho",
            ),
            AndroidNode(
                "",
                "Send",
                "grindr:id/send",
                "android.widget.Button",
                (100, 10, 120, 30),
                True,
                True,
                "",
            ),
        ]
        with (
            patch.object(self.android, "_validate_device"),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_hierarchy", return_value=nodes),
        ):
            with self.assertRaisesRegex(RuntimeError, "já contém um rascunho"):
                self.android.prepare_reply("emulator-5554", "Ana", "Oi")

    def test_reply_shell_arguments_quote_ascii_meta_characters(self):
        text = "Ola! Oi; $HOME 'voce'"
        title = AndroidNode(
            "Ana", "", "", "android.widget.TextView", (0, 0, 10, 10), False, True, "Ana"
        )
        field = AndroidNode(
            "",
            "",
            "grindr:id/compose",
            "android.widget.EditText",
            (0, 10, 100, 30),
            False,
            True,
            "",
        )
        button = AndroidNode(
            "",
            "Send",
            "grindr:id/send",
            "android.widget.Button",
            (100, 10, 120, 30),
            True,
            True,
            "",
        )
        last_message = AndroidNode(
            "Última mensagem",
            "",
            "",
            "android.widget.TextView",
            (0, 40, 100, 60),
            False,
            True,
            "",
        )
        initial = [title, last_message, field, button]
        draft = [
            title,
            last_message,
            AndroidNode(
                text,
                "",
                "grindr:id/compose",
                "android.widget.EditText",
                (0, 10, 100, 30),
                False,
                True,
                text,
            ),
            button,
        ]
        final = [
            title,
            AndroidNode(
                text, "", "", "android.widget.TextView", (0, 70, 100, 90), False, True, text
            ),
            field,
            button,
        ]
        with (
            patch.object(self.android, "_validate_device"),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_hierarchy", side_effect=[initial, draft, final]),
            patch.object(self.android, "_tap") as tap,
            patch.object(self.android, "_run", return_value="") as run,
        ):
            result = self.android.send_approved_reply(
                "emulator-5554",
                "Ana",
                text,
                "Ana\nÚltima mensagem",
            )

        self.assertIn("Resposta enviada", result)
        self.assertEqual(tap.call_args_list[0].args, ("emulator-5554", field))
        self.assertEqual(tap.call_args_list[1].args, ("emulator-5554", button))
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["-s", "emulator-5554", "shell", "input", "text"])
        self.assertEqual(shlex.split(command[5]), ["Ola!%sOi;%s$HOME%s'voce'"])

    def test_reply_input_plan_rejects_non_ascii_before_any_device_action(self):
        title = AndroidNode(
            "Ana", "", "", "android.widget.TextView", (0, 0, 10, 10), False, True, "Ana"
        )
        field = AndroidNode(
            "",
            "",
            "grindr:id/compose",
            "android.widget.EditText",
            (0, 10, 100, 30),
            False,
            True,
            "",
        )
        button = AndroidNode(
            "",
            "Send",
            "grindr:id/send",
            "android.widget.Button",
            (100, 10, 120, 30),
            True,
            True,
            "",
        )
        with (
            patch.object(self.android, "_validate_device"),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(self.android, "_hierarchy", return_value=[title, field, button]),
            patch.object(self.android, "_tap") as tap,
            patch.object(self.android, "_run") as run,
        ):
            with self.assertRaisesRegex(ValueError, "caracteres não ASCII"):
                self.android.send_approved_reply(
                    "emulator-5554",
                    "Ana",
                    "Você",
                    "Ana",
                )

        tap.assert_not_called()
        run.assert_not_called()

    def test_reply_rejects_reserved_space_escape_before_touching_device(self):
        with self.assertRaisesRegex(ValueError, "sequência reservada"):
            self.android.prepare_reply("emulator-5554", "Ana", "texto%scom")

    def test_prepare_reply_rejects_non_ascii_before_reading_device(self):
        with (
            patch.object(self.android, "_validate_device") as validate,
            patch.object(self.android, "_foreground") as foreground,
            patch.object(self.android, "_hierarchy") as hierarchy,
        ):
            with self.assertRaisesRegex(ValueError, "caracteres não ASCII"):
                self.android.prepare_reply("emulator-5554", "Ana", "você?")

        validate.assert_not_called()
        foreground.assert_not_called()
        hierarchy.assert_not_called()

    def test_reply_does_not_tap_send_when_android_text_differs(self):
        text = "Ola, Ana!"
        title = AndroidNode(
            "Ana", "", "", "android.widget.TextView", (0, 0, 10, 10), False, True, "Ana"
        )
        field = AndroidNode(
            "",
            "",
            "grindr:id/compose",
            "android.widget.EditText",
            (0, 10, 100, 30),
            False,
            True,
            "",
        )
        button = AndroidNode(
            "",
            "Send",
            "grindr:id/send",
            "android.widget.Button",
            (100, 10, 120, 30),
            True,
            True,
            "",
        )
        modified_draft = AndroidNode(
            "Ola Ana!",
            "",
            "grindr:id/compose",
            "android.widget.EditText",
            (0, 10, 100, 30),
            False,
            True,
            "Ola Ana!",
        )
        with (
            patch.object(self.android, "_validate_device"),
            patch.object(self.android, "_foreground", return_value=GRINDR_PACKAGE),
            patch.object(
                self.android,
                "_hierarchy",
                side_effect=[[title, field, button], [title, modified_draft, button]],
            ),
            patch.object(self.android, "_tap") as tap,
            patch.object(self.android, "_run", return_value=""),
        ):
            with self.assertRaisesRegex(RuntimeError, "não corresponde exatamente"):
                self.android.send_approved_reply("emulator-5554", "Ana", text, "Ana")

        self.assertEqual(tap.call_count, 1)

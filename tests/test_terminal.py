import unittest
from contextlib import nullcontext
from io import StringIO
from os import terminal_size
from unittest.mock import patch

from prompt_toolkit.document import Document

from assistente.terminal import (
    SlashCommandCompleter,
    escape_terminal_text,
    format_tool_inventory,
    parse_plugin_mentions,
    parse_terminal_help_command,
    print_response,
    print_status,
    read_prompt,
    show_welcome,
    terminal_help,
)


class TerminalWelcomeTests(unittest.TestCase):
    def test_escape_terminal_text_preserves_unicode_and_escapes_controls(self):
        self.assertEqual(
            escape_terminal_text("Olá\x1b[31m\nMundo"),
            r"Olá\x1b[31m\x0aMundo",
        )
        self.assertEqual(
            escape_terminal_text("mensagem longa", limit=4),
            "mens [texto truncado; 14 caracteres no total]",
        )

    def test_slash_completer_lists_hubs_and_providers(self):
        completer = SlashCommandCompleter()
        suggestions = {
            completion.text for completion in completer.get_completions(Document("/", 1), None)
        }
        self.assertIn("/hub", suggestions)
        self.assertIn("/hub/github/copilot", suggestions)
        self.assertIn("/hub/aws/bedrock", suggestions)
        self.assertIn("/hub/n8n", suggestions)
        self.assertTrue(
            {
                "/projeto",
                "/provedores",
                "/hubs",
                "/login-copilot",
                "/status",
                "/ferramentas",
                "/execucoes",
                "/plugins",
                "/plugins adicionar",
                "/skills",
                "/seguranca",
                "/auditoria-seguranca",
                "/auditoria-elevacao",
                "/aprendizado",
                "/memoria",
                "/exportar",
                "/ajuda",
                "/limpar",
                "/sair",
            }.issubset(suggestions)
        )

    def test_slash_completer_filters_as_route_is_typed(self):
        completer = SlashCommandCompleter()
        suggestions = [
            completion.text
            for completion in completer.get_completions(Document("/hub/google", 11), None)
        ]
        self.assertEqual(
            suggestions,
            ["/hub/google/gemini", "/hub/google/antigravity"],
        )

    def test_plugin_completer_offers_at_mentions_in_add_plugin_command(self):
        completer = SlashCommandCompleter()
        completions = list(completer.get_completions(Document("/plugins adicionar @", 20), None))
        completion_map = {
            item.display_text: (item.text, str(item.display_meta)) for item in completions
        }

        self.assertIn("@stripe", completion_map)
        self.assertIn("@microsoft", completion_map)
        self.assertIn("@cloudflare", completion_map)
        self.assertNotIn("@onedrive", completion_map)
        self.assertEqual(completion_map["@stripe"][0], "/plugins adicionar @stripe")
        self.assertIn("STRIPE_SECRET_KEY", completion_map["@stripe"][1])
        prefix_completions = list(
            completer.get_completions(Document("/plugins adicionar @goog", 23), None)
        )
        self.assertEqual(
            [item.display_text for item in prefix_completions],
            ["@google-workspace"],
        )

    def test_typing_at_alone_offers_plugin_completions(self):
        completer = SlashCommandCompleter()
        completions = list(completer.get_completions(Document("@", 1), None))

        self.assertTrue(completions)
        self.assertIn("@stripe", {item.display_text for item in completions})
        self.assertIn("@microsoft", {item.display_text for item in completions})
        self.assertEqual(
            [item.display_text for item in completer.get_completions(Document("@goog", 5), None)],
            ["@google-workspace"],
        )

    def test_plugin_autocomplete_preserves_multiple_mentions(self):
        completer = SlashCommandCompleter()
        for prompt in (
            "/plugins adicionar @stripe @goo",
            "@stripe @goo",
        ):
            with self.subTest(prompt=prompt):
                completions = list(completer.get_completions(Document(prompt, len(prompt)), None))
                self.assertEqual(len(completions), 1)
                self.assertEqual(completions[0].display_text, "@google-workspace")
                self.assertEqual(
                    completions[0].text,
                    prompt[:-4] + "@google-workspace",
                )

    def test_standalone_plugin_mentions_parse_for_add_selection_flow(self):
        self.assertEqual(parse_plugin_mentions("@"), [])
        self.assertEqual(
            parse_plugin_mentions("@stripe @github @Stripe"),
            ["stripe", "github"],
        )
        self.assertIsNone(parse_plugin_mentions("use @stripe for payments"))
        self.assertIsNone(parse_plugin_mentions("@stripe bad"))

    def test_computer_use_and_browser_are_available_in_plugin_suggestions(self):
        completer = SlashCommandCompleter()
        suggestions = {
            item.display_text for item in completer.get_completions(Document("@", 1), None)
        }
        self.assertIn("@computer-use", suggestions)
        self.assertIn("@navegador-use", suggestions)

    def test_advanced_help_has_topics_examples_and_approval_guidance(self):
        help_text = terminal_help()
        self.assertIn("/ajuda <tema>", help_text)
        self.assertIn("/projeto", help_text)
        self.assertIn("CEREBRO_PROJECT_ROOT", terminal_help("projeto"))
        self.assertIn("aprovações existentes", help_text)
        self.assertIn("MCP", terminal_help("integracoes"))
        self.assertIn("confirmação", terminal_help("seguranca"))
        self.assertIn("Temas disponíveis", terminal_help("unknown"))

    def test_tool_inventory_uses_live_tools_and_supports_category_filters(self):
        tools = [
            {
                "function": {
                    "name": "project_search",
                    "description": "Busca arquivos do projeto.",
                }
            },
            {
                "function": {
                    "name": "browser_open",
                    "description": "Abre uma página no navegador.",
                }
            },
            {
                "function": {
                    "name": "mcp_cloudflare_list",
                    "description": "[MCP:cloudflare] Lista recursos.",
                }
            },
            {
                "function": {
                    "name": "remote_terminal_start",
                    "description": "Abre um terminal remoto.",
                }
            },
            {
                "function": {
                    "name": "notebooklm_login",
                    "description": "Inicia o login NotebookLM.",
                }
            },
        ]
        inventory = format_tool_inventory(tools)
        browser_tools = format_tool_inventory(tools, "navegador")
        terminal_tools = format_tool_inventory(tools, "terminal")

        self.assertIn("project_search", inventory)
        self.assertIn("browser_open", inventory)
        self.assertIn("mcp_cloudflare_list", inventory)
        self.assertIn("browser_open", browser_tools)
        self.assertIn("remote_terminal_start", terminal_tools)
        self.assertIn("notebooklm_login", terminal_tools)
        self.assertNotIn("project_search", browser_tools)
        self.assertIn("Categorias:", format_tool_inventory(tools, "invalid"))
        self.assertIn(
            "Nenhuma ferramenta disponível",
            format_tool_inventory(tools, "desktop"),
        )
        self.assertIn(
            "aprovações existentes",
            inventory,
        )

    def test_terminal_help_commands_parse_exact_command_and_optional_filter(self):
        self.assertEqual(parse_terminal_help_command("/ajuda projeto"), ("/ajuda", "projeto"))
        self.assertEqual(
            parse_terminal_help_command(" /ferramentas navegador "),
            ("/ferramentas", "navegador"),
        )
        self.assertEqual(parse_terminal_help_command("/ajuda"), ("/ajuda", ""))
        self.assertIsNone(parse_terminal_help_command("me ajude com o projeto"))

    def test_welcome_contains_centered_cyan_ring_and_title_without_decoration(self):
        output = StringIO()
        with patch(
            "assistente.terminal.shutil.get_terminal_size",
            return_value=terminal_size((80, 24)),
        ):
            show_welcome(stream=output)

        lines = output.getvalue().splitlines()
        ring_lines = lines[:-3]
        self.assertTrue(ring_lines)
        self.assertTrue(all(set(line) <= {" ", "█"} for line in ring_lines))
        self.assertTrue(all(len(line) <= 80 for line in ring_lines))
        title = "CÉREBRO CENTRAL"
        title_line = next(line for line in lines if title in line)
        self.assertEqual(title_line, " " * ((80 - len(title)) // 2) + title)
        self.assertNotIn("╔", output.getvalue())
        self.assertNotIn("\033[", output.getvalue())

    def test_ring_adapts_to_narrow_terminals(self):
        output = StringIO()
        with patch(
            "assistente.terminal.shutil.get_terminal_size",
            return_value=terminal_size((20, 24)),
        ):
            show_welcome(stream=output)

        self.assertTrue(all(len(line) <= 20 for line in output.getvalue().splitlines()))
        self.assertIn("CÉREBRO CENTRAL", output.getvalue())

    def test_ring_never_exceeds_terminal_width(self):
        for width, height in ((1, 1), (12, 8), (20, 12), (40, 24), (80, 24), (120, 40)):
            with self.subTest(width=width, height=height):
                output = StringIO()
                with patch(
                    "assistente.terminal.shutil.get_terminal_size",
                    return_value=terminal_size((width, height)),
                ):
                    show_welcome(stream=output)

                self.assertTrue(all(len(line) <= width for line in output.getvalue().splitlines()))
                self.assertLessEqual(len(output.getvalue().splitlines()), height)

    def test_ring_scales_with_terminal_height(self):
        rendered = []
        for height in (12, 24, 40):
            output = StringIO()
            with patch(
                "assistente.terminal.shutil.get_terminal_size",
                return_value=terminal_size((120, height)),
            ):
                show_welcome(stream=output)
            rendered.append(sum("█" in line for line in output.getvalue().splitlines()))

        self.assertLess(rendered[0], rendered[1])
        self.assertLess(rendered[1], rendered[2])

    def test_response_is_visually_distinct_but_plain_when_not_tty(self):
        output = StringIO()
        with patch(
            "assistente.terminal.shutil.get_terminal_size",
            return_value=terminal_size((30, 24)),
        ):
            print_response("Cerebro Central:", "Olá.", stream=output)
        rendered = output.getvalue().splitlines()
        self.assertEqual(rendered[0], "")
        self.assertTrue(rendered[1].startswith("┌ Cerebro Central: "))
        self.assertIn("│ Olá.", rendered[2])
        self.assertTrue(rendered[3].endswith("┘"))

    def test_response_uses_cyan_on_tty(self):
        class TTYOutput(StringIO):
            def isatty(self):
                return True

        output = TTYOutput()
        with patch(
            "assistente.terminal.shutil.get_terminal_size",
            return_value=terminal_size((30, 24)),
        ):
            print_response("Cerebro Central:", "Olá.", stream=output)
        self.assertIn("\033[36m┌ Cerebro Central: ", output.getvalue())
        self.assertIn("\033[96m│ Olá.", output.getvalue())

    def test_status_is_yellow_on_tty(self):
        class TTYOutput(StringIO):
            def isatty(self):
                return True

        output = TTYOutput()
        print_status("Assistente pronto", stream=output)
        self.assertEqual(output.getvalue(), "\033[1;33mAssistente pronto\033[0m\n")

    def test_welcome_displays_one_logo_without_cursor_or_screen_controls(self):
        class TTYOutput(StringIO):
            def isatty(self):
                return True

        output = TTYOutput()
        with (
            patch(
                "assistente.terminal.shutil.get_terminal_size",
                return_value=terminal_size((80, 24)),
            ),
            patch("assistente.terminal.sys.stdout", output),
        ):
            show_welcome()
        self.assertIn("\033[36m", output.getvalue())
        self.assertIn("\033[1;38;5;51m", output.getvalue())
        self.assertNotIn("\033[?25l", output.getvalue())
        self.assertNotIn("\033[2J", output.getvalue())

    def test_interactive_welcome_renders_one_static_frame(self):
        class TTYOutput(StringIO):
            def isatty(self):
                return True

        output = TTYOutput()
        with (
            patch("assistente.terminal.sys.stdout", output),
            patch(
                "assistente.terminal.shutil.get_terminal_size",
                return_value=terminal_size((40, 12)),
            ) as get_size,
        ):
            show_welcome()

        self.assertEqual(get_size.call_count, 1)
        self.assertEqual(output.getvalue().count("CÉREBRO CENTRAL"), 1)
        self.assertNotIn("\033[2J", output.getvalue())
        self.assertNotIn("\033[?25l", output.getvalue())
        self.assertIn("CÉREBRO CENTRAL", output.getvalue())

    def test_welcome_propagates_terminal_rendering_errors(self):
        class TTYOutput(StringIO):
            def isatty(self):
                return True

        output = TTYOutput()
        with (
            patch("assistente.terminal.sys.stdout", output),
            patch(
                "assistente.terminal.shutil.get_terminal_size",
                side_effect=RuntimeError("resize failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "resize failed"):
                show_welcome()

        self.assertEqual(output.getvalue(), "")

    def test_user_prompt_uses_blue_style(self):
        with (
            patch("assistente.terminal.PromptSession") as prompt_session,
            patch("assistente.terminal.patch_stdout", return_value=nullcontext()) as patch_output,
        ):
            with patch("assistente.terminal.getpass.getuser", return_value="italosantos"):
                with patch("assistente.terminal.socket.gethostname", return_value="macbook.local"):
                    prompt_session.return_value.prompt.return_value = "Olá"
                    self.assertEqual(read_prompt("\nVocê> "), "Olá")

        patch_output.assert_called_once_with()
        (prompt,) = prompt_session.return_value.prompt.call_args.args
        self.assertEqual(prompt[1], ("class:user", "italosantos@macbook>"))
        self.assertIsNotNone(prompt_session.return_value.prompt.call_args.kwargs["style"])

    def test_color_is_disabled_when_no_color_is_set(self):
        class TTYOutput(StringIO):
            def isatty(self):
                return True

        output = TTYOutput()
        with patch.dict("os.environ", {"NO_COLOR": ""}):
            show_welcome(stream=output)

        self.assertNotIn("\033[", output.getvalue())


if __name__ == "__main__":
    unittest.main()

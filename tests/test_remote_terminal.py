import unittest
from unittest.mock import AsyncMock, Mock, patch

from assistente import __main__ as app
from assistente.remote_terminal import remote_terminal_command


class RemoteTerminalTests(unittest.TestCase):
    def test_ssh_vm_uses_only_validated_host_alias(self):
        with patch("assistente.remote_terminal.shutil.which", return_value="/usr/bin/ssh"):
            command = remote_terminal_command("ssh-vm", "prod-vm")

        self.assertEqual(command, ["/usr/bin/ssh", "-tt", "prod-vm"])

    def test_google_cloud_shell_uses_active_gcloud_without_authorizing_session(self):
        with patch("assistente.remote_terminal.shutil.which", return_value="/usr/bin/gcloud"):
            command = remote_terminal_command("google-cloud-shell")

        self.assertEqual(
            command,
            ["/usr/bin/gcloud", "cloud-shell", "ssh", "--ssh-flag=-tt"],
        )
        self.assertNotIn("--authorize-session", command)

    def test_remote_command_rejects_untrusted_targets_and_providers(self):
        for target in ("--proxy-command=x", "host;id", "user@host", "../host", ""):
            with self.subTest(target=target), self.assertRaises(ValueError):
                remote_terminal_command("ssh-vm", target)
        with self.assertRaises(ValueError):
            remote_terminal_command("google-cloud-shell", "unexpected-target")
        with self.assertRaises(ValueError):
            remote_terminal_command("arbitrary-command-provider")

    def test_connection_requires_approval_and_starts_fixed_argv(self):
        process_manager = Mock()
        process_manager.start.return_value = "Processo iniciado"
        with (
            patch.object(
                app,
                "remote_terminal_command",
                return_value=["/usr/bin/ssh", "-tt", "prod-vm"],
            ) as build_command,
        ):
            result = app.asyncio.run(
                app.invoke_tool(
                    "remote_terminal_start",
                    {"provider": "ssh-vm", "target": "prod-vm"},
                    {},
                    approve=AsyncMock(return_value=True),
                    process_manager=process_manager,
                )
            )

        self.assertEqual(result, "Processo iniciado")
        build_command.assert_called_once_with("ssh-vm", "prod-vm")
        process_manager.start.assert_called_once_with(["/usr/bin/ssh", "-tt", "prod-vm"])

    def test_denied_connection_does_not_start_cloud_shell(self):
        process_manager = Mock()
        approve = AsyncMock(return_value=False)
        with patch.object(
            app,
            "remote_terminal_command",
            return_value=["/usr/bin/gcloud", "cloud-shell", "ssh"],
        ):
            result = app.asyncio.run(
                app.invoke_tool(
                    "remote_terminal_start",
                    {"provider": "google-cloud-shell"},
                    {},
                    approve=approve,
                    process_manager=process_manager,
                )
            )

        self.assertEqual(result, "Conexão remota cancelada.")
        process_manager.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()

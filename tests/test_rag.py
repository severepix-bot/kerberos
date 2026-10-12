import ipaddress
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistente.__main__ import invoke_tool
from assistente.rag import (
    LocalRagStore,
    fetch_public_document,
    validate_source_url,
)


class RagSourceValidationTests(unittest.TestCase):
    def test_accepts_allowlisted_https_source_with_public_dns(self):
        with patch(
            "assistente.rag._resolve_public_addresses",
            return_value=[],
        ):
            url = validate_source_url("https://csrc.nist.gov/pubs/sp/800/193/final")

        self.assertEqual(url, "https://csrc.nist.gov/pubs/sp/800/193/final")

    def test_rejects_unapproved_hosts_credentials_ip_and_query_parameters(self):
        for url in (
            "http://csrc.nist.gov/pubs/sp/800/193/final",
            "https://example.com/security",
            "https://user:password@csrc.nist.gov/pubs/sp/800/193/final",
            "https://127.0.0.1/",
            "https://csrc.nist.gov/pubs/sp/800/193/final?token=secret",
        ):
            with (
                self.subTest(url=url),
                patch(
                    "assistente.rag._resolve_public_addresses",
                    return_value=[],
                ),
                self.assertRaises(ValueError),
            ):
                validate_source_url(url)

    def test_rejects_a_source_that_resolves_to_private_addresses(self):
        with (
            patch(
                "assistente.rag.socket.getaddrinfo",
                return_value=[
                    (2, 1, 6, "", ("192.168.1.5", 443)),
                ],
            ),
            self.assertRaisesRegex(ValueError, "endereço não público"),
        ):
            validate_source_url("https://csrc.nist.gov/pubs/sp/800/193/final")

    def test_fetch_extracts_page_text_and_title_with_bounded_response(self):
        class Headers:
            def get_content_type(self):
                return "text/html"

        class Response:
            status = 200
            headers = Headers()

            def read(self, limit):
                self.limit = limit
                return b"<html><title>Official Guide</title><script>secret()</script><p>Secure boot</p></html>"

        class Connection:
            def __init__(self, *args):
                self.response = Response()

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                return self.response

            def close(self):
                pass

        with (
            patch(
                "assistente.rag._resolve_public_addresses",
                return_value=[ipaddress.ip_address("8.8.8.8")],
            ),
            patch("assistente.rag._PinnedHTTPSConnection", Connection),
        ):
            title, text = fetch_public_document("https://csrc.nist.gov/pubs/sp/800/193/final")

        self.assertEqual(title, "Official Guide")
        self.assertIn("Secure boot", text)
        self.assertNotIn("secret()", text)


class LocalRagStoreTests(unittest.TestCase):
    def test_persists_searchable_chunks_with_source_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "assistente.rag._resolve_public_addresses",
                return_value=[],
            ):
                store = LocalRagStore(Path(directory) / "rag.sqlite3")
                chunk_count = store.add_document(
                    "https://csrc.nist.gov/pubs/sp/800/193/final",
                    "NIST Firmware Resilience",
                    "Protect firmware against unauthorized changes. Detect changes and recover securely.",
                )
                results = store.search("unauthorized firmware")
                sources = store.sources()
                usage = store.usage()

                reopened = LocalRagStore(Path(directory) / "rag.sqlite3")
                persisted = reopened.search("recover securely")

        self.assertEqual(chunk_count, 1)
        self.assertEqual(usage, (1, 1))
        self.assertEqual(sources[0]["title"], "NIST Firmware Resilience")
        self.assertEqual(
            results[0]["url"],
            "https://csrc.nist.gov/pubs/sp/800/193/final",
        )
        self.assertEqual(len(persisted), 1)

    def test_reindex_replaces_content_and_delete_removes_all_indexed_data(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "assistente.rag._resolve_public_addresses",
                return_value=[],
            ),
        ):
            store = LocalRagStore(Path(directory) / "rag.sqlite3")
            url = "https://csrc.nist.gov/pubs/sp/800/193/final"
            store.add_document(url, "Guide", "Firmware resilience guidance.")
            store.add_document(url, "Updated guide", "Recovery controls and testing.")

            self.assertEqual(store.search("resilience"), [])
            self.assertEqual(len(store.search("recovery")), 1)
            store.delete_all()

            self.assertEqual(store.sources(), [])
            self.assertEqual(store.usage(), (0, 0))

    def test_search_prioritizes_matching_source_titles(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "assistente.rag._resolve_public_addresses",
                return_value=[],
            ),
        ):
            store = LocalRagStore(Path(directory) / "rag.sqlite3")
            store.add_document(
                "https://www.nist.gov/cyberframework",
                "NIST Cybersecurity Framework",
                "Govern risk and protect systems.",
            )
            store.add_document(
                "https://ubuntu.com/security",
                "Ubuntu Security",
                "A cybersecurity framework for software packages.",
            )

            results = store.search("NIST Cybersecurity Framework", limit=2)

        self.assertEqual(results[0]["title"], "NIST Cybersecurity Framework")


class RagToolApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_ingestion_requires_approval_before_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "rag.sqlite3"
            with (
                patch("assistente.__main__.RAG_DATABASE", database),
                patch("assistente.__main__.fetch_public_document") as fetch,
            ):
                output = await invoke_tool(
                    "rag_index_official_source",
                    {"source": "nist-firmware"},
                    {},
                    approve=lambda _action: self._deny(),
                )

        self.assertEqual(output, "Ação cancelada pelo usuário.")
        fetch.assert_not_called()
        self.assertFalse(database.exists())

    async def test_approved_ingestion_fetches_only_the_selected_official_source(self):
        async def approve(_action: str) -> bool:
            return True

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "rag.sqlite3"
            with (
                patch("assistente.__main__.RAG_DATABASE", database),
                patch(
                    "assistente.__main__.fetch_public_document",
                    return_value=("NIST Firmware", "Recovery after firmware tampering."),
                ) as fetch,
                patch("assistente.rag._resolve_public_addresses", return_value=[]),
            ):
                output = await invoke_tool(
                    "rag_index_official_source",
                    {"source": "nist-firmware"},
                    {},
                    approve=approve,
                )
                sources = LocalRagStore(database).sources()

        self.assertIn("indexada localmente", output)
        fetch.assert_called_once_with("https://csrc.nist.gov/pubs/sp/800/193/final")
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["title"], "NIST Firmware")

    async def _deny(self) -> bool:
        return False


if __name__ == "__main__":
    unittest.main()

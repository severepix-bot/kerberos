import http.client
import ipaddress
import os
import re
import socket
import sqlite3
import ssl
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

MAX_RESPONSE_BYTES = 1_000_000
MAX_DOCUMENT_CHARS = 200_000
MAX_CHUNK_CHARS = 1200
MAX_CHUNKS_PER_DOCUMENT = 200
MAX_INDEXED_CHUNKS = 5000
MAX_RESULTS = 5
FETCH_TIMEOUT_SECONDS = 12

OFFICIAL_SOURCES = {
    "android": ("source.android.com", "https://source.android.com/docs/security/overview"),
    "apple": ("support.apple.com", "https://support.apple.com/guide/security/welcome/web"),
    "cisa": ("www.cisa.gov", "https://www.cisa.gov/topics/cyber-threats-and-response"),
    "cisa-5g": ("www.cisa.gov", "https://www.cisa.gov/resources-tools/resources/5g-strategy"),
    "google-saif": ("saif.google", "https://saif.google"),
    "nist-bluetooth": ("csrc.nist.gov", "https://csrc.nist.gov/pubs/sp/800/121/r2/final"),
    "nist-csf": ("www.nist.gov", "https://www.nist.gov/cyberframework"),
    "microsoft": ("learn.microsoft.com", "https://learn.microsoft.com/en-us/windows/security/"),
    "microsoft-ai-red-team": (
        "learn.microsoft.com",
        "https://learn.microsoft.com/en-us/security/ai-red-team/",
    ),
    "nist-ai-rmf": ("www.nist.gov", "https://www.nist.gov/itl/ai-risk-management-framework"),
    "nist-genai-profile": (
        "www.nist.gov",
        "https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence",
    ),
    "nist-firmware": ("csrc.nist.gov", "https://csrc.nist.gov/pubs/sp/800/193/final"),
    "nist-incident-response": ("csrc.nist.gov", "https://csrc.nist.gov/pubs/sp/800/61/r3/final"),
    "nist-iot-baseline": ("csrc.nist.gov", "https://csrc.nist.gov/pubs/ir/8259/a/final"),
    "nist-pqc": ("csrc.nist.gov", "https://csrc.nist.gov/projects/post-quantum-cryptography"),
    "nist-radioid": ("csrc.nist.gov", "https://csrc.nist.gov/pubs/sp/800/98/final"),
    "nist-things-networks": ("csrc.nist.gov", "https://csrc.nist.gov/pubs/sp/800/183/final"),
    "nist-iot": ("csrc.nist.gov", "https://csrc.nist.gov/pubs/sp/800/213/final"),
    "nist-lte": ("csrc.nist.gov", "https://csrc.nist.gov/pubs/sp/800/187/final"),
    "nist-wlan": ("csrc.nist.gov", "https://csrc.nist.gov/pubs/sp/800/153/final"),
    "owasp-top-10": ("owasp.org", "https://owasp.org/projects/top-ten"),
    "itu-radio-propagation": ("www.itu.int", "https://www.itu.int/rec/R-REC-P.1817/en"),
    "ubuntu": ("ubuntu.com", "https://ubuntu.com/security"),
}

_ALLOWED_HOSTS = {host for host, _ in OFFICIAL_SOURCES.values()} | {
    "csrc.nist.gov",
    "www.nist.gov",
}
_PRIVATE_ADDRESS = re.compile(r"(?i)(?:^|\.)(?:localhost|local|internal|test)$")
_BLOCKED_IP_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "100::/64",
        "2001:db8::/32",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        normalized = " ".join(data.split())
        if normalized and not self._skip_depth:
            self.parts.append(normalized)
            if self._in_title:
                self.title_parts.append(normalized)


def _resolve_public_addresses(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        addresses = list(
            {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            }
        )
    except OSError as exc:
        raise ValueError("Não foi possível validar o endereço público da fonte.") from exc
    if not addresses or any(
        not address.is_global or any(address in network for network in _BLOCKED_IP_NETWORKS)
        for address in addresses
    ):
        raise ValueError("A fonte resolveu para endereço não público; acesso recusado.")
    return addresses


def _public_host(host: str) -> bool:
    normalized = host.rstrip(".").lower()
    if normalized not in _ALLOWED_HOSTS or _PRIVATE_ADDRESS.search(normalized):
        return False
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        return False
    _resolve_public_addresses(normalized)
    return True


def validate_source_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2000:
        raise ValueError("Informe uma URL HTTPS válida de uma fonte oficial allowlisted.")
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or parsed.fragment
        or not _public_host(host)
    ):
        raise ValueError(
            "Apenas HTTPS em domínios oficiais allowlisted é permitido; "
            "endereços locais, IPs e credenciais na URL são recusados."
        )
    if parsed.query:
        raise ValueError("Parâmetros de consulta não são permitidos em URLs de fontes.")
    normalized = urllib.parse.urlunsplit(("https", host.lower(), parsed.path or "/", "", ""))
    return normalized


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        timeout: int,
    ) -> None:
        self._tls_context = ssl.create_default_context()
        self._address = str(address)
        super().__init__(host, port=443, timeout=timeout, context=self._tls_context)

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._address, self.port),
            self.timeout,
        )
        self.sock = self._tls_context.wrap_socket(raw_socket, server_hostname=self.host)


def fetch_public_document(url: str) -> tuple[str, str]:
    normalized_url = validate_source_url(url)
    parsed_url = urllib.parse.urlsplit(normalized_url)
    host = parsed_url.hostname
    if host is None:
        raise ValueError("A URL da fonte não tem hostname.")
    address = _resolve_public_addresses(host)[0]
    connection = _PinnedHTTPSConnection(host, address, FETCH_TIMEOUT_SECONDS)
    path = urllib.parse.urlunsplit(("", "", parsed_url.path, parsed_url.query, ""))
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Host": host,
                "User-Agent": "Kerberos-Local-RAG/1.0",
                "Accept": "text/html, text/plain",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(
                f"A fonte respondeu HTTP {response.status}; redirecionamentos recusados."
            )
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "text/plain"}:
            raise ValueError(f"Tipo de conteúdo não suportado: {content_type}.")
        body = response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        connection.close()
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("A resposta excede o limite de tamanho permitido.")
    text = body.decode("utf-8", errors="replace").replace("\ufffd", "")
    if content_type == "text/html":
        parser = _TextExtractor()
        parser.feed(text)
        text = " ".join(parser.parts)
        title = " ".join(parser.title_parts)[:300] or host
    else:
        title = urllib.parse.urlsplit(normalized_url).path.rsplit("/", 1)[-1] or "Documento"
    text = " ".join(text.split())[:MAX_DOCUMENT_CHARS]
    title = " ".join(title.replace("\ufffd", "").split())[:300]
    if not text:
        raise ValueError("A página não contém texto indexável.")
    return title, text


def _chunks(text: str) -> list[str]:
    words = text.split()
    chunks: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + len(word) + 1 > MAX_CHUNK_CHARS:
            chunks.append(current)
            current = ""
            if len(chunks) >= MAX_CHUNKS_PER_DOCUMENT:
                break
        current = f"{current} {word}".strip()
    if current and len(chunks) < MAX_CHUNKS_PER_DOCUMENT:
        chunks.append(current)
    return chunks


class LocalRagStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("O banco RAG não pode ser um link simbólico.")
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_sources (
                    id INTEGER PRIMARY KEY,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    indexed_at TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks USING fts5(
                    source_id UNINDEXED,
                    body,
                    tokenize = 'unicode61'
                )
                """
            )
        if os.name != "nt":
            self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def add_document(self, url: str, title: str, text: str) -> int:
        normalized_url = validate_source_url(url)
        safe_title = " ".join(title.split())[:300] or "Documento sem título"
        pieces = _chunks(" ".join(text.split())[:MAX_DOCUMENT_CHARS])
        if not pieces:
            raise ValueError("O documento não contém texto indexável.")
        indexed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT id FROM rag_sources WHERE url = ?", (normalized_url,)
            ).fetchone()
            if previous:
                source_id = int(previous["id"])
                total = int(connection.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0])
                previous_count = int(
                    connection.execute(
                        "SELECT chunk_count FROM rag_sources WHERE id = ?", (source_id,)
                    ).fetchone()[0]
                )
                if total - previous_count + len(pieces) > MAX_INDEXED_CHUNKS:
                    raise ValueError(
                        "O índice atingiu o limite; remova fontes antigas antes de atualizar."
                    )
                connection.execute("DELETE FROM rag_chunks WHERE source_id = ?", (source_id,))
                connection.execute(
                    "UPDATE rag_sources SET title = ?, indexed_at = ?, chunk_count = ? WHERE id = ?",
                    (safe_title, indexed_at, len(pieces), source_id),
                )
            else:
                total = int(connection.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0])
                if total + len(pieces) > MAX_INDEXED_CHUNKS:
                    raise ValueError(
                        "O índice atingiu o limite; remova fontes antigas antes de adicionar."
                    )
                cursor = connection.execute(
                    "INSERT INTO rag_sources (url, title, indexed_at, chunk_count) "
                    "VALUES (?, ?, ?, ?)",
                    (normalized_url, safe_title, indexed_at, len(pieces)),
                )
                if cursor.lastrowid is None:
                    raise sqlite3.DatabaseError("Não foi possível registrar a fonte RAG.")
                source_id = cursor.lastrowid
            connection.executemany(
                "INSERT INTO rag_chunks (source_id, body) VALUES (?, ?)",
                [(source_id, piece) for piece in pieces],
            )
        return len(pieces)

    def search(self, query: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValueError("A consulta RAG deve conter de 1 a 1000 caracteres.")
        if isinstance(limit, bool) or not 1 <= limit <= MAX_RESULTS:
            raise ValueError(f"O limite de resultados deve ficar entre 1 e {MAX_RESULTS}.")
        terms = list(dict.fromkeys(re.findall(r"[\w-]{2,}", query.casefold(), flags=re.UNICODE)))
        if not terms:
            return []
        searched_terms = terms[:30]
        match_query = " OR ".join('"' + term.replace('"', '""') + '"' for term in searched_terms)
        title_match = " OR ".join("lower(s.title) LIKE ?" for _ in searched_terms)
        term_parameters = tuple(f"%{term}%" for term in searched_terms)
        with self._connect() as connection:
            rows = list(
                connection.execute(
                    """
                SELECT c.rowid AS chunk_id, s.title, s.url, s.indexed_at, c.body,
                       bm25(rag_chunks) AS relevance
                FROM rag_chunks AS c
                JOIN rag_sources AS s ON s.id = c.source_id
                WHERE rag_chunks MATCH ?
                ORDER BY relevance
                LIMIT 100
                """,
                    (match_query,),
                ).fetchall()
            )
            title_rows = connection.execute(
                """
                SELECT c.rowid AS chunk_id, s.title, s.url, s.indexed_at, c.body,
                       0.0 AS relevance
                FROM rag_chunks AS c
                JOIN rag_sources AS s ON s.id = c.source_id
                WHERE """
                + title_match
                + """
                LIMIT 100
                """,
                term_parameters,
            ).fetchall()
        seen_ids = {int(row["chunk_id"]) for row in rows}
        rows.extend(row for row in title_rows if int(row["chunk_id"]) not in seen_ids)
        ranked: list[tuple[int, float, dict[str, Any]]] = []
        for row in rows:
            item = dict(row)
            body_terms = set(re.findall(r"[\w-]{2,}", item["body"].casefold(), flags=re.UNICODE))
            title_terms = set(re.findall(r"[\w-]{2,}", item["title"].casefold(), flags=re.UNICODE))
            score = sum(term in body_terms for term in terms[:30]) + 3 * sum(
                term in title_terms for term in terms[:30]
            )
            ranked.append((score, float(item["relevance"]), item))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [item for _, _, item in ranked[:limit]]

    def sources(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT title, url, indexed_at, chunk_count FROM rag_sources "
                "ORDER BY indexed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def usage(self) -> tuple[int, int]:
        with self._connect() as connection:
            sources, chunks = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(chunk_count), 0) FROM rag_sources"
            ).fetchone()
        return int(sources), int(chunks)

    def delete_all(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM rag_chunks")
            connection.execute("DELETE FROM rag_sources")


def official_source_list() -> str:
    return "\n".join(f"- {name}: {url}" for name, (_, url) in OFFICIAL_SOURCES.items())

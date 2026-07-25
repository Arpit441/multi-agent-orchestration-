"""Organisation knowledge base: upload docs, retrieve relevant chunks for agents."""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokenize(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", text.lower()) if t}


def chunk_text(text: str, *, size: int = 900, overlap: int = 120) -> list[str]:
    text = re.sub(r"\r\n?", "\n", text).strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        # Prefer break on paragraph/newline
        window = text[start:end]
        if end < len(text):
            br = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "))
            if br > size // 3:
                end = start + br + 1
                window = text[start:end]
        chunks.append(window.strip())
        if end >= len(text):
            break
        start = max(0, end - overlap)
    return [c for c in chunks if c]


def extract_text_from_bytes(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith((".txt", ".md", ".markdown", ".csv")):
        return data.decode("utf-8", errors="replace")
    if name.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            import io

            reader = PdfReader(io.BytesIO(data))
            parts = []
            for page in reader.pages:
                parts.append(page.extract_text() or "")
            return "\n".join(parts).strip()
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Could not read PDF: {exc}") from exc
    raise ValueError("Unsupported file type. Use .txt, .md, .csv, or .pdf")


@dataclass
class KnowledgeDoc:
    doc_id: str
    filename: str
    title: str
    char_count: int
    chunk_count: int
    created_at: str


class KnowledgeStore:
    """SQLite-backed document store with simple lexical retrieval (no embeddings)."""

    def __init__(self, db_path: str | Path = "data/knowledge.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    doc_id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    FOREIGN KEY(doc_id) REFERENCES documents(doc_id)
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
                """
            )

    def add_document(self, *, filename: str, content: str, title: str | None = None) -> KnowledgeDoc:
        content = content.strip()
        if len(content) < 20:
            raise ValueError("Document is too short (need at least ~20 characters of text).")
        if len(content) > 500_000:
            raise ValueError("Document is too large (max ~500k characters).")

        doc_id = str(uuid.uuid4())
        title = (title or Path(filename).stem).strip() or filename
        chunks = chunk_text(content)
        created = _utcnow()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO documents (doc_id, filename, title, content, char_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (doc_id, filename, title, content, len(content), created),
            )
            for i, chunk in enumerate(chunks):
                conn.execute(
                    """
                    INSERT INTO chunks (chunk_id, doc_id, filename, chunk_index, content)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), doc_id, filename, i, chunk),
                )

        return KnowledgeDoc(
            doc_id=doc_id,
            filename=filename,
            title=title,
            char_count=len(content),
            chunk_count=len(chunks),
            created_at=created,
        )

    def list_documents(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT d.doc_id, d.filename, d.title, d.char_count, d.created_at,
                       COUNT(c.chunk_id) AS chunk_count
                FROM documents d
                LEFT JOIN chunks c ON c.doc_id = d.doc_id
                GROUP BY d.doc_id
                ORDER BY d.created_at DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_document(self, doc_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            _ = cur
            cur2 = conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            return cur2.rowcount > 0

    def retrieve(self, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chunk_id, doc_id, filename, chunk_index, content FROM chunks"
            ).fetchall()
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            c_tokens = _tokenize(row["content"])
            if not c_tokens:
                continue
            overlap = len(q_tokens & c_tokens)
            if overlap == 0:
                continue
            # Jaccard-ish score favoring overlap density
            score = overlap / (len(q_tokens) ** 0.5)
            scored.append((score, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, row in scored[:top_k]:
            results.append(
                {
                    "filename": row["filename"],
                    "chunk_index": row["chunk_index"],
                    "score": round(score, 3),
                    "content": row["content"],
                }
            )
        return results

    def format_context(self, query: str, *, top_k: int = 5) -> str:
        hits = self.retrieve(query, top_k=top_k)
        if not hits:
            docs = self.list_documents()
            if not docs:
                return (
                    "(No organisation documents uploaded yet. "
                    "Agents will use only the built-in demo policy.)"
                )
            return (
                "(No strongly matching chunks for this query. "
                f"{len(docs)} document(s) are in the knowledge base; "
                "agents should still follow uploaded policies when relevant.)"
            )
        parts = []
        for i, hit in enumerate(hits, start=1):
            parts.append(
                f"[{i}] From `{hit['filename']}` (score={hit['score']}):\n{hit['content']}"
            )
        return "\n\n".join(parts)


_store: KnowledgeStore | None = None


def get_knowledge_store(db_path: str | Path | None = None) -> KnowledgeStore:
    global _store
    if _store is None:
        path = db_path or "data/knowledge.db"
        _store = KnowledgeStore(path)
    return _store


def set_knowledge_store(store: KnowledgeStore | None) -> None:
    global _store
    _store = store

"""Knowledge base retrieval tests."""

from agent_orchestrator.knowledge import KnowledgeStore


def test_upload_and_retrieve(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    doc = store.add_document(
        filename="policy.md",
        content=(
            "Duplicate charges: refund the extra charge within 1-2 business days "
            "if Order ID is verified. Refunds above $50 require human approval."
        ),
    )
    assert doc.chunk_count >= 1
    assert len(store.list_documents()) == 1

    hits = store.retrieve("I was charged twice for Pro, need refund Order 123", top_k=3)
    assert hits
    assert "refund" in hits[0]["content"].lower() or "charge" in hits[0]["content"].lower()

    ctx = store.format_context("duplicate charge refund")
    assert "policy.md" in ctx

    assert store.delete_document(doc.doc_id) is True
    assert store.list_documents() == []

"""Tests for agent thinking extraction."""

from agent_orchestrator.nodes.thinking import (
    extract_thinking_from_payload,
    split_thinking_text,
)


def test_split_thinking_text():
    thinking, answer = split_thinking_text(
        "<<<THINKING>>>\nI will focus on market size first.\n<<<END_THINKING>>>\n# Report\nHello"
    )
    assert "market size" in thinking
    assert answer.startswith("# Report")


def test_split_thinking_text_without_markers():
    thinking, answer = split_thinking_text("plain answer")
    assert thinking == ""
    assert answer == "plain answer"


def test_extract_thinking_from_payload():
    assert extract_thinking_from_payload({"thinking": "check refund policy", "score": 8}) == (
        "check refund policy"
    )
    assert extract_thinking_from_payload({"score": 8}) == ""


def test_thinking_suffix_is_same_call_instructions():
    from agent_orchestrator.nodes.thinking import thinking_system_suffix

    text = thinking_system_suffix(json_mode=False)
    assert "THINKING" in text
    assert "JSON" not in text or "Do not wrap" in text


def test_extract_json_tolerates_raw_newlines_in_strings():
    from agent_orchestrator.llm import extract_json

    raw = '{\n  "thinking": "line1\nline2",\n  "score": 8\n}'
    payload = extract_json(raw)
    assert payload["score"] == 8
    assert "line1" in payload["thinking"]
    assert "line2" in payload["thinking"]

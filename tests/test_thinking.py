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


def test_split_thinking_open_marker_only():
    """Model forgot the closing marker — still recover the thinking."""
    thinking, answer = split_thinking_text(
        "<<<THINKING>>>\nCompare pricing tiers first.\n\n# Report\nBody text"
    )
    assert "pricing tiers" in thinking
    assert answer.startswith("# Report")


def test_split_thinking_prefix_style():
    """Model used a 'Thinking:' lead-in instead of markers."""
    thinking, answer = split_thinking_text(
        "**Thinking:** I will outline the key risks.\n\nHere is the full answer."
    )
    assert "key risks" in thinking
    assert answer.startswith("Here is the full answer")


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


def test_extract_json_salvages_broken_researcher_payload():
    from agent_orchestrator.llm import extract_json

    # Truncated / broken JSON that still contains the useful fields.
    raw = (
        '{\n  "thinking": "Focus on scarcity trends.\n'
        '  "research_brief": "Global water scarcity overview",\n'
        '  "search_queries": ["water scarcity 2024", "desalination investment"]\n'
    )
    payload = extract_json(raw)
    assert "water scarcity" in payload["research_brief"].lower() or "Global" in payload["research_brief"]
    assert isinstance(payload.get("search_queries"), list)
    assert len(payload["search_queries"]) >= 1

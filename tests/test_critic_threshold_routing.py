"""Research critic routes: auto-deliver, auto-revise, or human when below threshold."""

from agent_orchestrator.core.state import State
from agent_orchestrator.examples.research_report_pipeline import (
    CRITIC_SCORE_THRESHOLD,
    MAX_AUTO_REVISIONS,
    _critic_passes,
    _needs_auto_revise,
    _needs_human_review,
)


def test_high_score_passes_without_human():
    state = State(data={"score": CRITIC_SCORE_THRESHOLD, "revision_count": 0})
    assert _critic_passes(state) is True
    assert _needs_auto_revise(state) is False
    assert _needs_human_review(state) is False


def test_low_score_auto_revises_while_budget_remains():
    state = State(data={"score": CRITIC_SCORE_THRESHOLD - 1, "revision_count": 1})
    assert _critic_passes(state) is False
    assert _needs_auto_revise(state) is True
    assert _needs_human_review(state) is False


def test_low_score_after_max_revisions_needs_human():
    state = State(
        data={
            "score": 4,
            "revision_count": MAX_AUTO_REVISIONS,
            "approved": False,
        }
    )
    assert _critic_passes(state) is False
    assert _needs_auto_revise(state) is False
    assert _needs_human_review(state) is True


def test_score_from_critic_output():
    state = State(
        data={"critic_output": {"score": 9, "approved": True}, "revision_count": 0}
    )
    assert _critic_passes(state) is True

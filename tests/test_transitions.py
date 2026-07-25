"""Tests for run status transition legality."""

import pytest

from agent_orchestrator.core.errors import IllegalStateTransition
from agent_orchestrator.core.state import RunStatus, assert_transition


def test_legal_transitions():
    assert_transition(RunStatus.PENDING, RunStatus.RUNNING)
    assert_transition(RunStatus.RUNNING, RunStatus.PAUSED)
    assert_transition(RunStatus.RUNNING, RunStatus.RETRYING)
    assert_transition(RunStatus.RETRYING, RunStatus.RUNNING)
    assert_transition(RunStatus.PAUSED, RunStatus.RUNNING)
    assert_transition(RunStatus.RUNNING, RunStatus.COMPLETED)
    assert_transition(RunStatus.RUNNING, RunStatus.FAILED)


def test_illegal_transitions_rejected():
    with pytest.raises(IllegalStateTransition):
        assert_transition(RunStatus.COMPLETED, RunStatus.RUNNING)
    with pytest.raises(IllegalStateTransition):
        assert_transition(RunStatus.FAILED, RunStatus.RUNNING)
    with pytest.raises(IllegalStateTransition):
        assert_transition(RunStatus.PENDING, RunStatus.PAUSED)
    with pytest.raises(IllegalStateTransition):
        assert_transition(RunStatus.PAUSED, RunStatus.COMPLETED)

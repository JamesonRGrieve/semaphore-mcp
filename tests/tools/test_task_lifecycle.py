# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for agent-attributed starts and force-escalating stops."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from semaphore_mcp.tools.task_lifecycle import (
    TaskLifecycleTools,
    agent_of,
    normalize_agent_id,
    stamp_message,
)


@pytest.fixture
def lifecycle():
    semaphore = MagicMock()
    task_tools = MagicMock()
    task_tools.run_task = AsyncMock(return_value={"task": {"id": 1}})
    task_tools.run_tofu_state_rm = AsyncMock(return_value={"mode": "list"})
    task_tools.confirm_task = AsyncMock(return_value={"confirmed": True})
    task_tools.bulk_stop_tasks = AsyncMock(return_value={"confirmation_required": True})
    return TaskLifecycleTools(semaphore, task_tools)


class TestAgentId:
    @pytest.mark.parametrize(
        "given",
        ["c1bd5c", "[c1bd5c]", "OMG [c1bd5c]", "  C1BD5C ", "Some Name [c1bd5c]"],
    )
    def test_accepts_listagents_forms(self, given):
        assert normalize_agent_id(given) == "c1bd5c"

    @pytest.mark.parametrize("given", [None, "", "OMG", "zz12", "c1bd5c extra", 5469])
    def test_rejects_anything_else(self, given):
        with pytest.raises(ValueError, match="ListAgents"):
            normalize_agent_id(given)

    def test_stamp_replaces_an_earlier_stamp(self):
        assert stamp_message("c1bd5c", "PLAN x") == "[agent c1bd5c] PLAN x"
        assert (
            stamp_message("c1bd5c", "[agent abcdef] PLAN x") == "[agent c1bd5c] PLAN x"
        )
        assert stamp_message("c1bd5c", None) == "[agent c1bd5c]"
        assert agent_of("[agent c1bd5c] PLAN x") == "c1bd5c"
        assert agent_of("PLAN x") is None


class TestStarts:
    @pytest.mark.asyncio
    async def test_run_task_requires_agent_id(self, lifecycle):
        result = await lifecycle.run_task(agent_id="", template_id=1, project_id=1)
        assert result["error_type"] == "missing_agent_id"
        lifecycle.task_tools.run_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_task_stamps_message(self, lifecycle):
        result = await lifecycle.run_task(
            agent_id="OMG [c1bd5c]",
            template_id=1,
            project_id=1,
            message="PLAN x",
            follow=0,
        )
        kwargs = lifecycle.task_tools.run_task.call_args.kwargs
        assert kwargs["message"] == "[agent c1bd5c] PLAN x"
        assert kwargs["follow"] == 0
        assert result["agent_id"] == "c1bd5c"

    @pytest.mark.asyncio
    async def test_state_rm_stamps_default_label(self, lifecycle):
        await lifecycle.run_tofu_state_rm(
            agent_id="c1bd5c",
            tofu_root="opentofu/zephyrex",
            addresses=["a.b"],
            project_id=1,
        )
        kwargs = lifecycle.task_tools.run_tofu_state_rm.call_args.kwargs
        assert kwargs["message"] == (
            "[agent c1bd5c] tofu state list (preview): a.b in opentofu/zephyrex"
        )

    @pytest.mark.asyncio
    async def test_state_rm_requires_agent_id(self, lifecycle):
        result = await lifecycle.run_tofu_state_rm(
            agent_id="nope", tofu_root="r", addresses=["a"]
        )
        assert result["error_type"] == "missing_agent_id"
        lifecycle.task_tools.run_tofu_state_rm.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirm_requires_agent_and_records_both(self, lifecycle):
        refused = await lifecycle.confirm_task(agent_id=None, project_id=1, task_id=9)
        assert refused["error_type"] == "missing_agent_id"
        lifecycle.task_tools.confirm_task.assert_not_called()

        lifecycle.semaphore.get_task.return_value = {"message": "[agent abcdef] PLAN"}
        result = await lifecycle.confirm_task(
            agent_id="c1bd5c", project_id=1, task_id=9
        )
        lifecycle.task_tools.confirm_task.assert_awaited_once_with(1, 9, follow=600)
        assert result["confirmed_by_agent"] == "c1bd5c"
        assert result["started_by_agent"] == "abcdef"


class TestStop:
    @pytest.mark.asyncio
    async def test_graceful_stop_that_finishes_is_not_forced(self, lifecycle):
        lifecycle.semaphore.get_task.side_effect = [
            {"status": "running", "message": "[agent c1bd5c] x"},
            {"status": "stopped"},
        ]
        result = await lifecycle.stop_task(1, 5, poll_interval=0)
        assert result["stopped"] is True and result["forced"] is False
        assert result["started_by_agent"] == "c1bd5c"
        lifecycle.semaphore.stop_task.assert_called_once_with(1, 5)

    @pytest.mark.asyncio
    async def test_hung_stop_escalates_to_force(self, lifecycle):
        lifecycle.semaphore.get_task.side_effect = [
            {"status": "waiting"},
            {"status": "stopping"},
            {"status": "stopped"},
        ]
        result = await lifecycle.stop_task(1, 6, wait_timeout=0, poll_interval=0)
        assert result["stopped"] is True and result["forced"] is True
        lifecycle.semaphore.stop_task.assert_any_call(1, 6)
        lifecycle.semaphore.stop_task.assert_called_with(1, 6, force=True)

    @pytest.mark.asyncio
    async def test_stuck_stopping_is_forced_at_once(self, lifecycle):
        lifecycle.semaphore.get_task.side_effect = [
            {"status": "stopping"},
            {"status": "stopped"},
        ]
        result = await lifecycle.stop_task(1, 5469, poll_interval=0)
        assert result["forced"] is True and result["stopped"] is True
        lifecycle.semaphore.stop_task.assert_called_once_with(1, 5469, force=True)

    @pytest.mark.asyncio
    async def test_force_flag_skips_graceful(self, lifecycle):
        lifecycle.semaphore.get_task.side_effect = [
            {"status": "running"},
            {"status": "stopped"},
        ]
        result = await lifecycle.stop_task(1, 7, force=True, poll_interval=0)
        assert result["forced"] is True
        lifecycle.semaphore.stop_task.assert_called_once_with(1, 7, force=True)

    @pytest.mark.asyncio
    async def test_unreleased_after_force_reports_blocked_queue(self, lifecycle):
        lifecycle.semaphore.get_task.side_effect = [
            {"status": "stopping"},
            {"status": "stopping"},
        ]
        result = await lifecycle.stop_task(1, 8, wait_timeout=0, poll_interval=0)
        assert result["stopped"] is False
        assert "restarts" in result["message"]

    @pytest.mark.asyncio
    async def test_parked_plan_is_refused(self, lifecycle):
        lifecycle.semaphore.get_task.return_value = {"status": "waiting_confirmation"}
        result = await lifecycle.stop_task(1, 9)
        assert result["stopped"] is False
        lifecycle.semaphore.stop_task.assert_not_called()
        lifecycle.semaphore.reject_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_terminal_task_is_left_alone(self, lifecycle):
        lifecycle.semaphore.get_task.return_value = {"status": "success"}
        result = await lifecycle.stop_task(1, 10)
        assert result["stopped"] is True
        lifecycle.semaphore.stop_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_all_template_tasks_drives_each_active_one(self, lifecycle):
        lifecycle.semaphore.list_tasks.return_value = [
            {"id": 1, "template_id": 3, "status": "stopping"},
            {"id": 2, "template_id": 3, "status": "success"},
            {"id": 3, "template_id": 4, "status": "running"},
            {"id": 4, "template_id": 3, "status": "waiting_confirmation"},
        ]
        lifecycle.semaphore.get_task.side_effect = [
            {"status": "stopping"},
            {"status": "stopped"},
        ]
        result = await lifecycle.stop_all_template_tasks(1, 3)
        assert [r["task_id"] for r in result["results"]] == [1]
        lifecycle.semaphore.stop_all_template_tasks.assert_called_once_with(1, 3)
        lifecycle.semaphore.stop_task.assert_called_once_with(1, 1, force=True)

    @pytest.mark.asyncio
    async def test_bulk_stop_preview_and_execute(self, lifecycle):
        preview = await lifecycle.bulk_stop_tasks(1, [1])
        assert preview["confirmation_required"] is True
        lifecycle.semaphore.get_task.side_effect = [
            {"status": "stopping"},
            {"status": "stopped"},
        ]
        done = await lifecycle.bulk_stop_tasks(1, [1], confirm=True)
        assert done["summary"]["successful_stops"] == 1


@pytest.mark.asyncio
async def test_registered_start_tools_require_agent_id():
    """The tool schemas an agent sees make agent_id mandatory on every start."""
    from semaphore_mcp.server import SemaphoreMCPServer

    server = SemaphoreMCPServer("http://semaphore.invalid", "token")
    tools = {t.name: t for t in await server.mcp.list_tools()}
    for name in ("run_task", "run_tofu_state_rm", "confirm_task"):
        assert "agent_id" in tools[name].input_schema["required"], name
    assert "force" in tools["stop_task"].input_schema["properties"]

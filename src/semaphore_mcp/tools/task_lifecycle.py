# SPDX-License-Identifier: AGPL-3.0-or-later
"""Agent-attributed task starts and stops that actually finish.

Two operator rules (2026-11-06) live here, in a module of our own so the
upstream TaskTools surface stays rebasable:

1. **Every start names its agent.** run_task, run_tofu_state_rm and
   confirm_task take a mandatory ``agent_id``: the calling session's ListAgents
   ID (the hex in "This session is <name> [<id>]"). It is stamped into the
   task message as ``[agent <id>]``, so the owner of any queued or stuck task
   can be found and messaged.

2. **Stopping works.** A plain ``POST /tasks/{id}/stop`` moves a task to
   'stopping'. A task that was still queued (never started) can then sit in
   'stopping' indefinitely, holding its template's queue: Semaphore runs one
   task per template, and DELETE refuses a task still in the pool. Zephyrex
   task 5469 did this on 2026-11-05. Semaphore's answer is the force stop
   (``{"force": true}``), which marks the task 'stopped' and drops it from the
   pool. drive_to_terminal sends the graceful signal, waits, then escalates to
   the force stop. A task already in 'stopping' is forced at once: its graceful
   stop was sent earlier and did not finish.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import requests

from .base import BaseTool
from .tasks import (
    TASK_ACTIVE_POOL_STATES,
    TASK_PARKED_STATE,
    TASK_TERMINAL_STATES,
    TaskTools,
    default_state_rm_message,
)

logger = logging.getLogger(__name__)

# A ListAgents session ID is short lowercase hex ("c1bd5c"). Accept the bare ID,
# "[c1bd5c]", or the whole "Name [c1bd5c]" line, so a caller can paste either.
_AGENT_ID_RE = re.compile(r"(?:^|\[)([0-9a-f]{6,16})\]?$")
_AGENT_STAMP_RE = re.compile(r"^\[agent ([0-9a-f]{6,16})\]\s*")

STOPPING_STATE = "stopping"


def normalize_agent_id(agent_id: Any) -> str:
    """Return the bare ListAgents ID, or raise ValueError naming how to get one."""
    raw = str(agent_id or "").strip().lower()
    match = _AGENT_ID_RE.search(raw)
    if not match:
        raise ValueError(
            "agent_id is required: pass your session's ListAgents ID (the hex in "
            "brackets on the first line of ListAgents, 'This session is <name> "
            f"[<id>]'). Got {agent_id!r}."
        )
    return match.group(1)


def stamp_message(agent_id: str, message: str | None) -> str:
    """Prefix a task message with ``[agent <id>]``, replacing any earlier stamp."""
    body = _AGENT_STAMP_RE.sub("", (message or "").strip())
    return f"[agent {agent_id}] {body}".strip()


def agent_of(message: str | None) -> str | None:
    """The agent ID stamped on a task message, if any."""
    match = _AGENT_STAMP_RE.match(message or "")
    return match.group(1) if match else None


async def _poll_terminal(
    semaphore: Any, project_id: int, task_id: int, timeout: float, poll_interval: float
) -> dict[str, Any]:
    """Poll until the task is terminal or gone; one poll at least, then until timeout."""
    deadline = time.time() + timeout
    while True:
        try:
            task = semaphore.get_task(project_id, task_id)
        except requests.exceptions.HTTPError as e:
            if "404" in str(e):
                return {"status": "deleted", "terminal": True}
            raise
        status = task.get("status")
        if status in TASK_TERMINAL_STATES:
            return {"status": status, "terminal": True}
        if time.time() >= deadline:
            return {"status": status, "terminal": False}
        await asyncio.sleep(poll_interval)


async def drive_to_terminal(
    semaphore: Any,
    project_id: int,
    task_id: int,
    *,
    status: str | None,
    signal: str | None = "stop",
    wait_timeout: float = 30,
    force_timeout: float = 15,
    poll_interval: float = 2,
    escalate: bool = True,
    force_now: bool = False,
) -> dict[str, Any]:
    """Signal a task to stop (or reject a parked plan) and see it reach a terminal state.

    Args:
        semaphore: API client
        project_id, task_id: the task
        status: its current status (the caller's probe), used to skip the
            graceful step for a task already in 'stopping'
        signal: "stop" or "reject" for the graceful step; None if the caller
            already sent it
        wait_timeout: seconds to wait after the graceful signal
        force_timeout: seconds to wait after the force stop
        escalate: send the force stop when the graceful wait runs out
        force_now: skip the graceful step and force at once

    Returns:
        {"status", "terminal", "signal", "forced"}
    """
    forced = False
    if status == STOPPING_STATE and escalate:
        force_now = True  # its graceful stop was already sent and did not finish

    if not force_now:
        if signal == "reject":
            semaphore.reject_task(project_id, task_id)
        elif signal == "stop":
            semaphore.stop_task(project_id, task_id)
        result = await _poll_terminal(
            semaphore, project_id, task_id, wait_timeout, poll_interval
        )
        if result["terminal"] or not escalate:
            return {**result, "signal": signal, "forced": False}

    logger.warning(
        "Task %s did not stop gracefully (status %s); sending force stop",
        task_id,
        status,
    )
    semaphore.stop_task(project_id, task_id, force=True)
    forced = True
    result = await _poll_terminal(
        semaphore, project_id, task_id, force_timeout, poll_interval
    )
    return {**result, "signal": signal, "forced": forced}


class TaskLifecycleTools(BaseTool):
    """The registered start/stop tools: agent-attributed starts, stops that finish."""

    def __init__(self, semaphore_client: Any, task_tools: TaskTools):
        super().__init__(semaphore_client)
        self.task_tools = task_tools

    async def run_task(
        self,
        agent_id: str,
        template_id: int,
        project_id: int | None = None,
        environment: dict[str, str] | None = None,
        limit: str | None = None,
        dry_run: bool | None = None,
        diff: bool | None = None,
        debug: bool | None = None,
        playbook: str | None = None,
        git_branch: str | None = None,
        message: str | None = None,
        arguments: list[str] | None = None,
        inventory_id: int | None = None,
        follow: int = 600,
    ) -> dict[str, Any]:
        """Run a task from a template and wait for it to finish.

        Args:
            agent_id: REQUIRED. Your session's ListAgents ID: the hex in brackets on the first line of ListAgents ("This session is <name> [<id>]"). Stamped into the task message as "[agent <id>]" so the task's owner can always be found.
            template_id: ID of the template to run
            project_id: Optional project ID (if not provided, will attempt to determine from template)
            environment: Optional environment variables for the task as dictionary
            limit: Restrict execution to specific hosts/groups (Ansible --limit)
            dry_run: Run without making changes (Ansible --check)
            diff: Show differences when changing files (Ansible --diff)
            debug: Enable verbose debug output
            playbook: Override playbook file path
            git_branch: Override git branch to use
            message: Task description/message (the agent stamp is prepended)
            arguments: Additional CLI arguments. MUST be a JSON array of strings, NOT a string. Example: ["-target=module.foo", "-refresh=false"].
            inventory_id: Override inventory to use
            follow: Max seconds to wait for a terminal state (success, error, waiting_confirmation); returns as soon as it gets there. 0 = fire and forget (default 600).

        Returns:
            Task execution result with web URLs and monitoring summary
        """
        try:
            agent = normalize_agent_id(agent_id)
        except ValueError as e:
            return {"error": str(e), "error_type": "missing_agent_id"}
        result = await self.task_tools.run_task(
            template_id=template_id,
            project_id=project_id,
            environment=environment,
            limit=limit,
            dry_run=dry_run,
            diff=diff,
            debug=debug,
            playbook=playbook,
            git_branch=git_branch,
            message=stamp_message(agent, message),
            arguments=arguments,
            inventory_id=inventory_id,
            follow=follow,
        )
        result["agent_id"] = agent
        return result

    async def run_tofu_state_rm(
        self,
        agent_id: str,
        tofu_root: str,
        addresses: list[str],
        remove: bool = False,
        project_id: int | None = None,
        template_id: int | None = None,
        template_name: str = "state-rm",
        message: str | None = None,
        follow: int = 600,
    ) -> dict[str, Any]:
        """Run a `tofu state rm` over the sanctioned Semaphore pipeline (bash utility template).

        Defaults to a list-only preview (remove=False); remove=True performs the
        removal, which makes tofu forget the entries (the real resources are
        untouched). Preview first.

        Args:
            agent_id: REQUIRED. Your session's ListAgents ID (the hex in brackets on the first line of ListAgents). Stamped into the task message.
            tofu_root: Repo-relative tofu root, e.g. "opentofu/zephyrex".
            addresses: Resource addresses, a JSON array of strings.
            remove: False (default) previews only; True removes.
            project_id: Project ID (needed to resolve the template by name).
            template_id: ID of the state-rm utility template.
            template_name: Name to resolve the template by (default "state-rm").
            message: Optional task label (the agent stamp is prepended).
            follow: Max seconds to wait for the task to finish (default 600).

        Returns:
            Mode, the task run result and the state output.
        """
        try:
            agent = normalize_agent_id(agent_id)
        except ValueError as e:
            return {"error": str(e), "error_type": "missing_agent_id"}
        label = message or default_state_rm_message(addresses or [], tofu_root, remove)
        result = await self.task_tools.run_tofu_state_rm(
            tofu_root=tofu_root,
            addresses=addresses,
            remove=remove,
            project_id=project_id,
            template_id=template_id,
            template_name=template_name,
            message=stamp_message(agent, label),
            follow=follow,
        )
        result["agent_id"] = agent
        return result

    async def confirm_task(
        self, agent_id: str, project_id: int, task_id: int, follow: int = 600
    ) -> dict[str, Any]:
        """Confirm a parked task, starting its apply.

        The runner re-plans at confirm from the task's persisted arguments, so a
        `-target` in them scopes the apply exactly like the parked plan. Review
        the parked plan (get_task_raw_output) first: every change line must match
        your accept list, and it must be `0 to destroy` unless a destroy is
        explicitly allowed.

        Args:
            agent_id: REQUIRED. Your session's ListAgents ID (the hex in brackets on the first line of ListAgents). Recorded as the confirmer.
            project_id: ID of the project
            task_id: ID of the parked task to confirm
            follow: Seconds to wait for the apply to finish (default 600). 0 = fire and forget.

        Returns:
            Confirm result with apply monitoring, plus who started and who confirmed it.
        """
        try:
            agent = normalize_agent_id(agent_id)
        except ValueError as e:
            return {"error": str(e), "error_type": "missing_agent_id"}
        started_by: str | None = None
        try:
            started_by = agent_of(
                self.semaphore.get_task(project_id, task_id).get("message")
            )
        except Exception:
            pass  # attribution is a label; never block the confirm on it
        logger.info(
            "Task %s confirmed by agent %s (started by %s)", task_id, agent, started_by
        )
        result = await self.task_tools.confirm_task(project_id, task_id, follow=follow)
        result["confirmed_by_agent"] = agent
        result["started_by_agent"] = started_by
        return result

    async def stop_task(
        self,
        project_id: int,
        task_id: int,
        force: bool = False,
        wait_timeout: int = 30,
        poll_interval: int = 2,
    ) -> dict[str, Any]:
        """Stop a task and see it reach a terminal state, force-stopping if it hangs.

        A queued task stopped gracefully can stick in 'stopping' and keep its
        template's queue blocked. This tool sends the graceful stop, waits up to
        wait_timeout seconds, then sends Semaphore's force stop. A task already
        in 'stopping' is forced at once. A parked plan (waiting_confirmation) is
        refused here: reject_task is the call for that.

        Args:
            project_id: ID of the project
            task_id: ID of the task to stop
            force: Force-stop at once, without the graceful attempt
            wait_timeout: Seconds to wait after the graceful stop before forcing, and after the force stop (default 30)
            poll_interval: Seconds between status polls (default 2)

        Returns:
            {"task_id", "stopped", "status", "forced", "message"}
        """
        try:
            try:
                task = self.semaphore.get_task(project_id, task_id)
            except requests.exceptions.HTTPError as e:
                if "404" in str(e):
                    return {
                        "task_id": task_id,
                        "stopped": False,
                        "message": f"Task #{task_id} not found.",
                    }
                raise
            status = task.get("status")
            if status in TASK_TERMINAL_STATES:
                return {
                    "task_id": task_id,
                    "stopped": True,
                    "status": status,
                    "forced": False,
                    "message": f"Task #{task_id} is already {status}.",
                }
            if status == TASK_PARKED_STATE:
                return {
                    "task_id": task_id,
                    "stopped": False,
                    "status": status,
                    "message": (
                        f"Task #{task_id} is a parked plan. Reject it with "
                        "reject_task (only on the operator's word for that task)."
                    ),
                }
            outcome = await drive_to_terminal(
                self.semaphore,
                project_id,
                task_id,
                status=status,
                wait_timeout=wait_timeout,
                force_timeout=wait_timeout,
                poll_interval=poll_interval,
                force_now=force,
            )
            how = "force-stopped" if outcome["forced"] else "stopped"
            return {
                "task_id": task_id,
                "stopped": outcome["terminal"],
                "status": outcome["status"],
                "forced": outcome["forced"],
                "started_by_agent": agent_of(task.get("message")),
                "message": (
                    f"Task #{task_id} {how} (status: {outcome['status']})."
                    if outcome["terminal"]
                    else f"Task #{task_id} is still '{outcome['status']}' after a "
                    "force stop; Semaphore did not release it. Its template "
                    "queue stays blocked until the Semaphore service restarts."
                ),
            }
        except Exception as e:
            self.handle_error(e, f"stopping task {task_id}")

    async def bulk_stop_tasks(
        self, project_id: int, task_ids: list[int], confirm: bool = False
    ) -> dict[str, Any]:
        """Stop several tasks, each through stop_task (graceful, then forced if it hangs).

        Args:
            project_id: ID of the project
            task_ids: Task IDs to stop
            confirm: Set to True to execute; False previews the tasks and statuses

        Returns:
            Preview, or per-task stop results with a summary
        """
        if not confirm:
            return await self.task_tools.bulk_stop_tasks(project_id, task_ids)
        results = []
        for task_id in task_ids:
            try:
                results.append(await self.stop_task(project_id, task_id))
            except Exception as e:
                results.append({"task_id": task_id, "stopped": False, "error": str(e)})
        stopped = sum(1 for r in results if r.get("stopped"))
        return {
            "bulk_operation_complete": True,
            "summary": {
                "total_tasks": len(task_ids),
                "successful_stops": stopped,
                "failed_stops": len(task_ids) - stopped,
            },
            "results": results,
        }

    async def stop_all_template_tasks(
        self, project_id: int, template_id: int
    ) -> dict[str, Any]:
        """Stop every active task of a template, force-stopping any that hang.

        Calls Semaphore's stop_all_tasks for the template, then drives each of
        its tasks still queued, running or stopping to a terminal state (parked
        plans are left for reject_task).

        Args:
            project_id: ID of the project
            template_id: ID of the template

        Returns:
            Per-task stop results
        """
        try:
            self.semaphore.stop_all_template_tasks(project_id, template_id)
            tasks = self.semaphore.list_tasks(project_id)
            task_list = tasks if isinstance(tasks, list) else tasks.get("tasks", [])
            active = [
                t["id"]
                for t in task_list
                if t.get("template_id") == template_id
                and t.get("status") in TASK_ACTIVE_POOL_STATES
            ]
            results = [await self.stop_task(project_id, tid) for tid in active]
            return {"template_id": template_id, "results": results}
        except Exception as e:
            self.handle_error(e, f"stopping all tasks for template {template_id}")

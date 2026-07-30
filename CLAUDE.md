# semaphore-mcp (fork) — Agent Operating Guide

> **STATUS: INTENT ONLY — no implementation yet.** This file is the design record
> for *our fork*, written before any code. It exists only on the `james` branch.

This is a fork of [`cloin/semaphore-mcp`](https://github.com/cloin/semaphore-mcp)
(AGPL-3.0-or-later), adapted to drive **Semaphore UI** — the sanctioned OpenTofu /
Ansible runner at `http://192.168.7.235`, project `1` — from an AI agent through
the **plan → confirm → apply** gate the lab's change-safety doctrine requires.

Paired with [`netbox-mcp`](../netbox-mcp): NetBox declares intent, Semaphore
realizes it. Together they replace the throwaway `sem_run.py` / `poll_*.sh` /
`wait_queue.sh` scripts agents currently rewrite every session.

---

## 0. Why we forked instead of building

Measured before deciding. Semaphore is the **second-largest Bash burn: 973 calls /
497 KB in a single session**, and `build-pipeline-service/SKILL.md` prescribes
`sem_run.py` *by name* while that script exists only in a scratchpad.

Upstream was assessed by reading the source, not the docs:

| | `cloin/semaphore-mcp` |
|---|---|
| License | **AGPL-3.0-or-later** — exact match to our policy |
| Health | 73★, 6 forks, `v1.0.4` (2026-05-18), 0 open PRs, 1 open issue |
| Structure | class-per-resource, client injected, shared `BaseTool`, DRY `_build_*_url` helpers |
| Hygiene | **0** `print()`, **0** bare `except:`, **1** `type: ignore`, full type annotations |
| Errors | 28 `except Exception` — all route to a centralized `self.handle_error(e, ctx)` |
| Tests | real suite including `e2e/` against a live Semaphore |

**The code is good.** Forking is cheap because quality was never the problem —
`register_tools()` registers each tool on its own line, so narrowing the surface is
deleting lines, not refactoring.

What made a fork (rather than upstream dependency) correct: **bus factor 1** (140 of
160 commits are one person), silent since 2026-05-19, and outside-PR throughput of
two ever — #26 took **95 days** to merge, #1 was closed unmerged. Our
confirm/reject work would plausibly sit for months, so we deploy our own branch and
treat upstream merges as a bonus.

---

## 1. Branch discipline — this is what keeps the fork cheap

```
upstream/main  (cloin)
   ├─ feat/confirm-reject        → PR upstream
   ├─ feat/trim-tool-surface     → local-only (policy, not general interest)
   └─ feat/<next>                → PR upstream where generally useful
        ↓ rebase all onto upstream/main
      james   ← THE DEPLOYED BRANCH
```

- **`main` tracks `upstream/main` and stays clean.** Never commit to it.
- **Every change is its own `feat/*` branch**, scoped to one concern.
- **`james` is the rebase of those branches** onto current `upstream/main`. It is
  what gets deployed. Nothing is committed to `james` directly — a direct commit
  is lost at the next rebase. *(This file is the sole exception, by explicit
  operator instruction.)*
- **Refresh:** `git fetch upstream && rebase each feat/* onto upstream/main &&
  rebuild james`. Every PR that lands upstream **shrinks** our diff.

> **⛔ Keep the diff additive and surgical — this is load-bearing.**
> Do **NOT** retrofit SPDX headers across all 57 files, and do **NOT** enable
> `mypy --strict` repo-wide. Both would touch every file in the repo and
> permanently maximise rebase-conflict surface. The project is already
> AGPL-3.0-or-later at the `LICENSE` / `pyproject` level, so per-file SPDX buys
> nothing here.
>
> Apply our standards (SPDX, strict typing, coverage) to **modules we add only**.
> Prefer new files over edits to existing ones wherever the choice exists.

### 1.1 Branch creation is guarded

`git checkout -b` and `git switch -c` are in the operator's `settings.json` deny
list. `git branch <name>` (without `-f`) is permitted. Create branch pointers with
`git branch`, and build commits with plumbing (`write-tree` / `commit-tree` /
`update-ref`) which never moves `HEAD` or touches the working tree. Do not attempt
to work around the deny rule.

---

## 2. The gap we exist to close: `/confirm` + `/reject`

Semaphore's plan-then-apply gate is
`POST /api/project/{id}/tasks/{task_id}/{confirm,reject}`. **Upstream does not
implement it, and neither does any other third-party Semaphore MCP server**
(`gabrielbelli/semaphore-mcp`: 0★, created and last touched the same day).

> **Correction to a plausible misreading:** grepping `tools/tasks.py` for
> `confirm|reject` returns 9 hits. **They are all false positives** — a
> `confirm: bool = False` guard on *bulk stop*, plus `get_waiting_tasks` filtering
> status `"waiting"` (queued, **not** awaiting approval). Verified in source. The
> plan→apply gate is genuinely absent.

This matters because the entire lab doctrine turns on it: a scoped plan does not
guarantee a scoped apply, and an ungated `run_task` is exactly the
"apply-without-a-reviewed-plan" hazard the change-safety rules exist to prevent.
(2026-07-05: a 4-object change applied unscoped became a 47-change fleet outage.)

**`feat/confirm-reject`** adds `confirm_task` / `reject_task` to `TaskTools` plus
the matching `api.py` methods (~60 lines), and PRs upstream.

---

## 3. `feat/trim-tool-surface` — narrow by policy

Upstream exposes 27+ tools and is trending toward more breadth (its final commits
added access-key management, project backup/restore, events, project users, views).
We need roughly seven:

| Keep | Why |
|---|---|
| `list_templates`, `list_tasks`, `get_task` | discovery + polling |
| `run_task` | trigger a **plan** |
| `get_task_raw_output` | read the plan for review |
| `confirm_task`, `reject_task` | **the gate** (we add these) |

**Hide:** every `delete_*`, project backup/restore, access-key management, and
project/user/permission mutation. Trimming is deleting lines from
`register_tools()`.

### 3.1 ⛔ The guardrail gap this fork must not widen

`~/.claude/settings.json` denies `tofu apply*`, `ansible-playbook*`, `pvesh set:*`
and friends — **but those rules bind the `Bash` tool only.** MCP is a separate
surface they do not cover, so an MCP `run_task` or `delete_template` sails straight
past them.

Therefore: the trimmed surface **is** the guardrail. Never register a tool that
mutates a device or applies without passing through confirm. Treat every added
tool as a deliberate widening of a trust boundary.

---

## 4. Reach + auth

- **Semaphore:** `http://192.168.7.235`, project `1` — CT `74235`, VLAN 74,
  reachable directly from the laptop over `wg_zephyrex`.
- **Token:** `SEMAPHORE_API_TOKEN` env var (upstream's contract). Sourced from the
  local `pass` cache of OpenBao, per the `secrets-sync` skill. **Never in argv** —
  it leaks to the process list.
- Templates in project 1: `1 = zephyrex` (whole owner root, ONE shared `pg` state),
  `2 = omg`. Base args `["-refresh=false","-parallelism=1"]`,
  `allow_override_args_in_task: true`.

> **Shared-state warning:** template 1 converges the **entire** zephyrex workspace.
> A `-target` used only at plan time does **not** carry to the apply. Pin the apply
> to a saved plan file, or verify the apply phase carries the identical scope,
> before confirming.

---

## 5. Verification owed before this is called done

- Full round-trip: `run_task` (plan) → `get_task` → `get_task_raw_output` →
  `confirm_task` → apply completes.
- A second run → `reject_task` leaves the task **unapplied**. *This is the primitive
  no third-party server has.*
- The tool list exposed to the agent contains exactly the seven above — no
  `delete_*`, no access-key tools.
- Upstream's existing test suite still passes (`pytest`), unmodified.
- `git fetch upstream && rebase` reproduces `james` cleanly, proving the diff stayed
  rebasable.

---

## 6. Licensing

Fork inherits **AGPL-3.0-or-later**. Files **we add** carry
`# SPDX-License-Identifier: AGPL-3.0-or-later`. Vendored upstream files are left
untouched (see §1).

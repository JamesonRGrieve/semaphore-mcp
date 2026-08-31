<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# semaphore-mcp examples

## `state_rm.sh` — tofu state-rm utility template

`tofu state rm` is a CLI-only operation: Semaphore's tofu app runs only
init/plan/apply/destroy, so there is no REST endpoint for it. The
`run_tofu_state_rm` MCP tool therefore drives a dedicated **bash-app utility
template** that runs this script inside the sanctioned NetBox→Semaphore
pipeline (auditable, using the runner's pg backend creds) rather than hitting
shared state directly from a shell.

### One-time setup (operator)

1. **Place the script in the tofu repo** that Semaphore checks out, e.g.
   `opentofu/utils/state_rm.sh`, and commit it.
2. **Create a Semaphore template** in the project (project `1`):
   - **App:** `bash`
   - **Playbook / script:** the repo-relative script path (e.g.
     `opentofu/utils/state_rm.sh`)
   - **Name:** `state-rm` (the MCP tool resolves the template by this name when
     `template_id` is not passed)
   - **Allow CLI args override:** enabled (`allow_override_args_in_task: true`)
   - **Environment:** an environment that supplies the **pg backend
     connection** and any **state-encryption key** — mirror the environment the
     root's normal plan/apply template uses. State operations do not need
     provider auth, only backend access. If different roots use different
     state-encryption keys, give each its own template/environment.

### Usage (via MCP)

```jsonc
// Preview (default, list-only — nothing is removed):
run_tofu_state_rm(
  tofu_root = "opentofu/zephyrex",
  addresses = ["module.svc_bifrost_config.host_file.env"],
  project_id = 1
)

// Actually remove (after reviewing the preview):
run_tofu_state_rm(
  tofu_root = "opentofu/zephyrex",
  addresses = ["module.svc_bifrost_config.host_file.env"],
  remove = true,
  project_id = 1
)
```

The root's own `pg` backend `schema_name` isolates its state, so `tofu_root`
fully determines which state is touched. Removing a state entry only makes tofu
*forget* the resource — the real infrastructure is left untouched.

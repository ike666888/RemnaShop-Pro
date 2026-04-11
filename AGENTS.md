# Project instructions

## Deployment goal
This repository must support exactly one deployment method: Docker Compose.

## Deployment rules
- Docker Compose is the only supported deployment method.
- Do not introduce or preserve any non-Docker deployment path.
- Do not keep deprecated, fallback, or parallel install methods.
- The bootstrap script is the single entrypoint for install/uninstall workflows.

## API source of truth
Use `docs/remnawave-openapi.json` as the single source of truth for all Remnawave API integration work.

## API rules
- Do not invent endpoints.
- Do not invent request fields, response fields, query parameters, path parameters, or callback payloads.
- Do not guess API behavior when the OpenAPI document is unclear.
- If the current code conflicts with `docs/remnawave-openapi.json`, update the code to match the OpenAPI document.
- Before changing any API-related code, read the relevant parts of `docs/remnawave-openapi.json`.
- Prefer deriving request/response shapes, validation rules, and typed models directly from the OpenAPI document.
- If a required API behavior is missing or ambiguous in the OpenAPI document, say so explicitly in the task output instead of guessing.
- Keep API-related docs/examples aligned with the actual implementation.

## Done criteria for API changes
An API-related task is not complete unless all of the following are true:
- The implementation matches `docs/remnawave-openapi.json`
- No guessed endpoints or fields were introduced
- Runtime behavior matches the documented API contract
- Related docs/examples are updated to match the real implementation

## Debugging rules
- When fixing API-related bugs, inspect the real runtime traceback and the real code path first.
- Do not patch symptoms by adding fallback guesses for API fields or routes.
- Fix the underlying implementation so it matches the documented API contract.
- Distinguish clearly between:
  - direct runtime root causes
  - separate configuration, network, or environment issues
- Prefer minimal, targeted fixes over speculative rewrites.

## Bootstrap behavior
The repository must keep a single bootstrap entrypoint.

The bootstrap implementation must support:
- install
- uninstall

A fresh Debian or Ubuntu server must be able to start installation successfully with minimal manual prerequisites.

The bootstrap script must automatically install missing common base dependencies when possible, including:
- curl
- ca-certificates
- git
- bash
- tar
- gzip
- unzip
- jq

Do not leave common prerequisites as manual README steps.

## Required bootstrap UX
All end-user-facing bootstrap prompts and status messages should be Chinese.

During install, only these values are required interactively:
- ADMIN_ID
- BOT_TOKEN

The bootstrap script must write these values into `.env` automatically.

Do not leave `ADMIN_ID` and `BOT_TOKEN` as a normal manual post-install requirement.

For a first-time install:
- `.env` should be created from `.env.example` if missing
- `ADMIN_ID` and `BOT_TOKEN` must be treated as required fresh input
- template placeholder values must not be treated as real existing values to keep
- the script should not ask whether to keep template placeholders

Only ask whether to keep existing values if `.env` already contains real non-empty values from a prior installation.

Optional values such as:
- `PANEL_URL`
- `PANEL_TOKEN`
- `SUB_DOMAIN`
- `GROUP_UUID`
- `PANEL_VERIFY_TLS`

must remain optional during bootstrap install and must not block deployment.

## .env.example rules
`.env.example` must keep required install-time values empty by default:
- `ADMIN_ID=`
- `BOT_TOKEN=`

Do not ship fake placeholder values such as:
- `ADMIN_ID=123456789`
- `BOT_TOKEN=123456:ABCDEF`

## Install success criteria
Install must not report success merely because `docker compose up -d` returned successfully.

Install is only successful after the `remnashop` container reaches:
- `health=healthy`

If the container exits, restarts repeatedly, or becomes unhealthy, the script must report failure clearly and provide troubleshooting guidance.

## Uninstall safety rules
Uninstall must only remove RemnaShop-Pro resources:
- the `remnashop` compose stack
- related containers
- related local images created for this project
- related volumes
- the project directory such as `/opt/remnashop-pro`

Uninstall must never affect unrelated Docker containers, images, volumes, networks, or other Compose projects on the host.

Do not use broad cleanup commands such as:
- `docker system prune`
- global Docker cleanup that is not scoped to RemnaShop-Pro

In interactive uninstall mode:
- require explicit confirmation before destructive actions
- accept common confirmation values such as `YES`, `yes`, `Y`, `y`
- make the prompt text clear and Chinese

## Production image contents
The production image must exclude non-runtime repository files, including:
- `tests/`
- `docs/`
- `README.md`
- `AGENTS.md`
- `LICENSE`
- `.gitignore`
- `.env.example`

Keep these files in the Git repository, but do not include them in the production image.

Use the actual Docker build configuration, such as `.dockerignore`, to enforce this.

## Implementation-first rule
Do not solve tasks with documentation-only changes.

If behavior changes:
- update the real implementation first
- then update README and docs to match

This applies especially to:
- bootstrap behavior
- install/uninstall flow
- `.env` handling
- API integration
- Docker build contents

## Validation rules
Before considering a task complete, verify the real behavior where applicable:
- install flow works
- uninstall flow works safely
- required env input flow works
- production image excludes non-runtime files
- runtime behavior matches the OpenAPI contract
- the service reaches healthy state after startup

## Output expectations
When making changes, clearly report:
- which files were changed
- the real runtime root cause for the issue being fixed
- what behavior changed
- what validation was performed
- any remaining configuration issues that still require manual action

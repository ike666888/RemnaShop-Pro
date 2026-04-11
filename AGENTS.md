# Project instructions

## Deployment goal
This repository must support exactly one deployment method: Docker Compose.

## Non-negotiable requirements
- Completely remove the legacy standalone/server-install deployment flow.
- Do not keep deprecated, fallback, or parallel non-Docker installation methods.
- After this refactor, there must be exactly one supported installation method in the repository: Docker Compose.
- The final public installation method must be a curl-pipe-bash bootstrap flow.

## Required install flow
Implement a reviewed bootstrap script inside the repository and make the final user-facing install command:

curl -fsSL <raw-script-url> | bash

The bootstrap script should:
- install Docker if missing
- install Docker Compose if missing
- clone or update the repository into a sensible directory
- prepare required environment files from templates
- start the stack with Docker Compose
- print clear success and verification output

## Repository cleanup
- Remove all legacy install scripts, docs, commands, references, and examples related to non-Docker deployment.
- Update README and all setup/deployment docs so they describe only Docker Compose.
- Remove conflicting instructions and dead files.

## Change strategy
- Modify the existing repository rather than creating a second deployment structure.
- Preserve application behavior unless a change is required for Docker Compose-only deployment.
- Keep changes reviewable, but complete.

## Validation
- Add verification steps for startup and health checks.
- Report assumptions, checks run, and any remaining risks.

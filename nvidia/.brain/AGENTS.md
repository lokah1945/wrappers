# AGENTS Log

## Active Agents & Tasks

### Super-Orchestrator v9.0 (Self)
* **Status**: COMPLETED
* **Task**: Audit model routing logic and fix compatibility issues for third-party agents accessing the wrapper.
* **Accomplishments**:
  - Restored model mapping for Claude and GPT request paths to local NVIDIA NIM models (including chat, embedding, and rerank families) via `resolveTargetModel` in `src/index.js`.
  - Fixed a redirection bug where `z-ai/glm-5.2` was incorrectly falling back to `meta/llama-3.1-8b-instruct` due to verification safety filters rejecting it during cold-starts.
  - Adjusted `REQUEST_TIMEOUT_SEC` to 600 seconds in `.env` to accommodate higher time-to-first-token (TTFT) windows of reasoning models.
  - Added robust validation to handle compatibility logic without upstream 404s.
  - Successfully synced and tested modifications against remote VPS `172.16.102.11` where the `wrapper-nvidia` service has been restarted and verified clean.
  - Cleaned up the repository of old logs, development payload JSONs, and obsolete backup directories/files both locally and on the remote VPS.
  - Committed the changes and successfully pushed to the remote GitHub repository (`master` branch).

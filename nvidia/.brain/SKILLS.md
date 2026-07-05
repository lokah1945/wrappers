# SKILLS Log

## Knowledge Items & Runbook

### Upstream Model Mapping & Translation
* **Context**: External LLM agents (Claude Code, Hermes, Kilo, etc.) call standard Anthropic (`/v1/messages`) or OpenAI (`/v1/chat/completions`) endpoints. Upstream NVIDIA NIM accepts neither Anthropic model names nor OpenAI generic names (e.g., `gpt-4o`).
* **Skill**: Use `resolveTargetModel(requestedModel)` to cleanly route chat, embedding, and ranking requests to equivalent verified NVIDIA NIM models available in the cache pool, preventing upstream 404 errors.

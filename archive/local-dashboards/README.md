# Archived local dashboards

As of 2026-10-17, wrapper-local and model-fetcher-local HTML dashboards are
not part of the deployed UI. The single dashboard is `/` on port 9106.

Archived sources:
- `nvidia-python/dashboard.html`
- `nous/dashboard.html`
- `openrouter/dashboard.html`
- `common/dashboard_template.html`
- `model-fetcher/dashboard.py`
- `model-fetcher/catalog-dashboard.html`

The active wrapper and model-fetcher routes return HTTP 410 with a pointer to
the unified dashboard. The model-fetcher MCP and JSON API transports remain
available for their non-dashboard contracts.

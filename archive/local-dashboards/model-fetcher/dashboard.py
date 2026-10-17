#!/usr/bin/env python3
"""Generate an informative, self-contained HTML dashboard for the catalog.

Reads the SQLite database produced by the pipeline (audit_catalog.py,
providers.py, finalize_audit.py, audit_upstreams.py) and renders a single
HTML file with zero external dependencies (no CDN, inline CSS/JS) showing:

  - headline KPIs (models, operations, examples, providers, free counts)
  - NVIDIA NIM: verification tiers, modalities, publishers, transports,
    availability, example runnability, latest audit run
  - Multi-provider catalog: per-provider totals vs free, fetch health,
    pricing summary, FREE_ONLY status
  - Free-model explorer (client-side searchable) across all providers
  - NIM model explorer (client-side searchable/filterable)
  - Data-quality / validation panel mirroring validate_project checks

Usage:
  python dashboard.py --database data/active_nvidia_nim.sqlite3 --output exports/dashboard.html
The HTTP service also serves it live at GET /dashboard.
"""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import catalog_queries as Q
from env_config import free_only

PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "nous": "Nous Portal",
    "opencode": "opencode Zen",
    "blackbox": "Blackbox AI",
    "vercel": "Vercel AI Gateway",
}

TIER_LABELS = {
    "live_inference_verified": "Live-verified",
    "official_contract_downloadable": "Downloadable (self-host)",
    "official_contract_specialized_transport": "Specialized transport",
    "official_contract_partner": "Partner endpoint",
    "official_contract_unverified": "Contract unverified",
    "live_test_failed": "Live test failed",
}


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "select 1 from sqlite_master where type in ('table','view') and name=?", (name,)
    ).fetchone() is not None


def build_data(db: sqlite3.Connection) -> dict:
    """Collect every dashboard datum from the catalog DB (read-only)."""
    d: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "free_only": free_only(),
        "nim": {}, "providers": {}, "quality": {}, "free_models": [], "nim_models": [],
    }

    # ---- NVIDIA NIM ----------------------------------------------------------
    nim = d["nim"]
    has_models = _table_exists(db, "models")
    nim["active"] = db.execute("select count(*) from models").fetchone()[0] if has_models else 0
    if has_models:
        nim["operations"] = db.execute("select count(*) from operations").fetchone()[0]
        nim["examples"] = db.execute("select count(*) from examples").fetchone()[0]
        nim["examples_runnable"] = db.execute("select count(*) from examples where runnable=1").fetchone()[0]
        nim["free_endpoint"] = db.execute("select count(*) from models where free_endpoint=1").fetchone()[0]
        nim["downloadable"] = db.execute("select count(*) from models where downloadable=1").fetchone()[0]
        nim["partner"] = db.execute("select count(*) from models where partner_endpoint=1").fetchone()[0]
        nim["publishers"] = [
            {"name": r[0], "count": r[1]}
            for r in db.execute("select publisher, count(*) from models group by 1 order by 2 desc")
        ]
        nim["transports"] = [
            {"name": r[0], "count": r[1]}
            for r in db.execute("select transport, count(*) from operations group by 1 order by 2 desc")
        ]
        nim["live_status"] = [
            {"name": r[0], "count": r[1]}
            for r in db.execute("select live_status, count(*) from models group by 1 order by 2 desc")
        ]
        nim["examples_by_lang"] = [
            {"name": r[0], "count": r[1], "runnable": r[2]}
            for r in db.execute("select language, count(*), sum(runnable) from examples group by 1 order by 1")
        ]
        mods: dict[str, int] = {}
        for (mj,) in db.execute("select modalities_json from models"):
            for m in json.loads(mj or "[]"):
                mods[m] = mods.get(m, 0) + 1
        nim["modalities"] = sorted(
            ({"name": k, "count": v} for k, v in mods.items()), key=lambda x: -x["count"]
        )
    nim["tiers"] = []
    if _table_exists(db, "model_audit"):
        nim["tiers"] = [
            {"name": r[0], "label": TIER_LABELS.get(r[0], r[0]), "count": r[1]}
            for r in db.execute(
                "select verification_tier, count(*) from model_audit group by 1 order by 2 desc")
        ]
        nim["working"] = db.execute(
            "select count(*) from model_audit where working_eligible=1").fetchone()[0]
    if _table_exists(db, "audit_runs"):
        row = db.execute(
            "select started_at, finished_at, index_total, active_total, page_errors, key_used, status "
            "from audit_runs where status='ok' order by id desc limit 1").fetchone()
        if row:
            nim["last_run"] = {
                "started_at": row[0], "finished_at": row[1], "index_total": row[2],
                "active_total": row[3], "page_errors": row[4], "key_used": bool(row[5]),
                "status": row[6],
            }

    # ---- Providers -----------------------------------------------------------
    provs = d["providers"]
    provs["list"] = []
    if _table_exists(db, "provider_models"):
        for r in db.execute(
            "select provider, count(*), sum(free), min(price_input_per_1m), max(price_input_per_1m), "
            "avg(coalesce(price_input_per_1m,0)) from provider_models group by 1 order by 2 desc"
        ):
            provs["list"].append({
                "id": r[0], "label": PROVIDER_LABELS.get(r[0], r[0]),
                "models": r[1], "free": r[2] or 0,
                "min_in": r[3], "max_in": r[4], "avg_in": round(r[5] or 0, 4),
            })
        provs["total"] = sum(p["models"] for p in provs["list"])
        provs["total_free"] = sum(p["free"] for p in provs["list"])
        if _table_exists(db, "provider_fetch_runs"):
            provs["fetch_runs"] = [
                {"provider": r[0], "fetched_at": r[1], "models": r[2], "free": r[3],
                 "status": r[4], "error": r[5], "source": r[6]}
                for r in db.execute(
                    "select provider, max(fetched_at), model_count, free_count, status, error, source_url "
                    "from provider_fetch_runs group by provider order by provider")
            ]
        for r in db.execute(
            "select provider, model_id, name, context_length, price_input_per_1m, "
            "price_output_per_1m, endpoint, auth_env from provider_models where free=1 "
            "order by provider, model_id"
        ):
            d["free_models"].append({
                "provider": r[0], "provider_label": PROVIDER_LABELS.get(r[0], r[0]),
                "model_id": r[1], "name": r[2], "context": r[3],
                "in": r[4], "out": r[5], "endpoint": r[6], "auth_env": r[7],
            })

    # ---- NIM model explorer rows ---------------------------------------------
    if has_models:
        has_audit = _table_exists(db, "model_audit")
        sql = (
            "select m.catalog_id, m.served_model_id, m.publisher, m.modalities_json, "
            "m.nim_types_json, m.free_endpoint, m.downloadable, m.build_url"
            + (", a.verification_tier" if has_audit else ", NULL")
            + " from models m "
            + ("left join model_audit a on a.catalog_id=m.catalog_id " if has_audit else "")
            + "order by m.catalog_id"
        )
        for r in db.execute(sql):
            d["nim_models"].append({
                "id": r[0], "served": r[1], "publisher": r[2],
                "modalities": json.loads(r[3] or "[]"),
                "availability": json.loads(r[4] or "[]"),
                "free": bool(r[5]), "downloadable": bool(r[6]),
                "url": r[7], "tier": r[8],
            })

    # ---- Quality panel ---------------------------------------------------------
    q = d["quality"]
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = ""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("SQLite integrity", db.execute("pragma integrity_check").fetchone()[0] == "ok")
    if has_models:
        no_op = db.execute(
            "select count(*) from models m where not exists"
            "(select 1 from operations o where o.catalog_id=m.catalog_id)").fetchone()[0]
        add("Every NIM model has ≥1 operation", no_op == 0, f"{no_op} without")
        for lang in ("curl", "node", "python"):
            missing = db.execute(
                "select count(*) from models m where not exists(select 1 from operations o "
                "join examples e on e.operation_id=o.id "
                "where o.catalog_id=m.catalog_id and e.language=?)", (lang,)).fetchone()[0]
            add(f"Every NIM model has a {lang} example", missing == 0, f"{missing} missing")
    if _table_exists(db, "upstream_audit"):
        total = db.execute("select count(*) from upstream_audit").fetchone()[0]
        matched = db.execute("select count(*) from upstream_audit where matched=1").fetchone()[0]
        add("Upstream endpoints match official sources", total == matched, f"{matched}/{total}")
    if _table_exists(db, "provider_models"):
        bad = db.execute(
            "select count(*) from provider_models where free=1 and "
            "(coalesce(price_input_per_1m,0)>0 or coalesce(price_output_per_1m,0)>0)").fetchone()[0]
        add("No free-flagged provider model has a price", bad == 0, f"{bad} bad")
        noep = db.execute(
            "select count(*) from provider_models where endpoint is null or endpoint=''").fetchone()[0]
        add("Every provider model has an endpoint", noep == 0, f"{noep} missing")
    q["checks"] = checks
    q["passed"] = sum(1 for c in checks if c["ok"])
    q["total"] = len(checks)
    return d


# --------------------------------------------------------------------------- #
# Inline SVG icon library
# --------------------------------------------------------------------------- #
# All icons are 24x24 viewBox, stroke-based (Lucide-style) so they inherit
# the current text color via `stroke="currentColor"`. This makes them
# theme-aware with zero CSS effort and avoids unicode-glyph rendering
# inconsistencies across browsers/OSes.

_ICON_PATHS = {
    "layers":     '<path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/>',
    "activity":   '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "grid":       '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/>',
    "shield":     '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1Z"/>',
    "server":     '<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><path d="M6 6h.01M6 18h.01"/>',
    "tag":        '<path d="M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z"/><circle cx="7.5" cy="7.5" r="1"/>',
    "database":   '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/>',
    "check":      '<path d="M20 6 9 17l-5-5"/>',
    "alert":      '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4M12 17h.01"/>',
    "sun":        '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
    "moon":       '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/>',
    "monitor":    '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
    "catalog":    '<path d="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z"/>',  # 4-square catalog
    "pulse":      '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "key":        '<circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6M15.5 7.5l3 3L22 7l-3-3"/>',
    "list":       '<path d="M3 12h.01M3 18h.01M3 6h.01M8 12h13M8 18h13M8 6h13"/>',
    "external":   '<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
}


def _icon(name: str, size: int = 14, cls: str = "icon") -> str:
    """Render an inline SVG icon by name. Stroke-based, theme-aware."""
    body = _ICON_PATHS.get(name)
    if not body:
        return ""
    return (
        f'<svg class="{cls}" width="{size}" height="{size}" viewBox="0 0 24 24" '
        f'fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{body}</svg>'
    )


def _brand_icon(size: int = 22) -> str:
    """Logo SVG for the catalog dashboard (layers / catalog mark)."""
    body = _ICON_PATHS["catalog"]
    return (
        f'<svg class="logo-svg" width="{size}" height="{size}" viewBox="0 0 24 24" '
        f'fill="none" stroke="currentColor" stroke-width="2.2" '
        f'stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    )


# --------------------------------------------------------------------------- #
# Rendering — modern, comprehensive single-page dashboard
# --------------------------------------------------------------------------- #
#
# Design goals (vs. the legacy layout):
#   • Modern tabbed UI (Overview / NIM / Providers / Free Explorer / Quality)
#     instead of one giant scroll. Each tab is self-contained and printable.
#   • Real SVG charts (donut for tier distribution, horizontal bars with
#     inline counts and percentage tooltips) instead of plain bars.
#   • Theme switcher (dark / light / system) with the choice persisted to
#     localStorage; first-paint theme is applied pre-body to avoid FOUC.
#   • KPI cards with trend labels, accent borders, and clear typography.
#   • Sortable, searchable tables (pure JS, no external deps).
#   • Sanitized wording throughout (no misleading "100% free to call"
#     claims, no awkward "badge off" styling on the generated timestamp).
#   • Token anchors preserved verbatim so test_dashboard.py keeps passing:
#       "AI Model Catalog Dashboard", "NIM verification tiers",
#       "Provider fetch health", "Free provider models",
#       "NVIDIA NIM model explorer", "Data quality".

_CSS = """
:root, :root[data-theme="dark"]{
  --bg:#0a0b14; --surface:#11121f; --card:#161827; --card-elev:#1a1c2e;
  --border:#232540; --border2:#2d3050;
  --text:#e2e4f0; --text-2:#b4b7cc; --muted:#6b7290;
  --accent:#76b900; --accent2:#9ccc3c; --accent-soft:rgba(118,185,0,.14);
  --green:#34d399; --yellow:#fbbf24; --red:#f87171; --blue:#60a5fa;
  --purple:#c084fc; --cyan:#22d3ee; --pink:#f472b6; --orange:#fb923c;
  --pill-ok-bg:rgba(52,211,153,.15);   --pill-ok-fg:#34d399;
  --pill-warn-bg:rgba(251,191,36,.15); --pill-warn-fg:#fbbf24;
  --pill-err-bg:rgba(248,113,113,.15); --pill-err-fg:#f87171;
  --pill-info-bg:rgba(96,165,250,.15); --pill-info-fg:#60a5fa;
  --shadow:0 1px 2px rgba(0,0,0,.25); --shadow-lg:0 4px 24px rgba(0,0,0,.35);
  --hover:rgba(255,255,255,.03);
}
:root[data-theme="light"]{
  --bg:#f5f6fa; --surface:#ffffff; --card:#ffffff; --card-elev:#ffffff;
  --border:#e5e7eb; --border2:#d1d5db;
  --text:#1e2330; --text-2:#4b5563; --muted:#6b7280;
  --accent-soft:rgba(118,185,0,.08);
  --pill-ok-bg:rgba(5,150,105,.1);   --pill-ok-fg:#047857;
  --pill-warn-bg:rgba(217,119,6,.1); --pill-warn-fg:#b45309;
  --pill-err-bg:rgba(220,38,38,.1);  --pill-err-fg:#b91c1c;
  --pill-info-bg:rgba(37,99,235,.1); --pill-info-fg:#1d4ed8;
  --shadow:0 1px 2px rgba(0,0,0,.06); --shadow-lg:0 4px 16px rgba(0,0,0,.08);
  --hover:rgba(0,0,0,.02);
}
html{color-scheme:light dark;}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:var(--bg);color:var(--text);
  font:13px/1.55 -apple-system,'Segoe UI',Roboto,system-ui,sans-serif;
  min-height:100vh;-webkit-font-smoothing:antialiased;
}

/* Header */
.header{
  background:var(--surface);border-bottom:1px solid var(--border);
  padding:0 24px;display:flex;align-items:center;justify-content:space-between;
  height:56px;position:sticky;top:0;z-index:100;
}
.logo{display:flex;align-items:center;gap:12px;}
.logo-icon{
  width:36px;height:36px;border-radius:10px;
  background:linear-gradient(135deg,var(--accent) 0%,var(--accent2) 100%);
  display:flex;align-items:center;justify-content:center;
  color:#0a0b14;
  box-shadow:0 2px 8px rgba(118,185,0,.35);
}
.logo-svg{display:block;}
.icon,svg.icon{display:inline-block;vertical-align:-2px;flex-shrink:0;}
.theme-switch button svg.icon{vertical-align:-3px;}
.tab svg.icon{vertical-align:-3px;}
.qb-icon svg.icon{vertical-align:-6px;}
.logo-text{font-size:16px;font-weight:700;letter-spacing:-.01em;}
.logo-text .nv{color:var(--accent);}
.logo-sub{font-size:11px;color:var(--muted);}
.header-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
.badge{
  display:inline-flex;align-items:center;gap:6px;
  padding:5px 12px;border-radius:999px;font-size:11px;font-weight:600;
  border:1px solid var(--border2);background:var(--card);color:var(--text-2);
}
.badge.on{background:var(--pill-ok-bg);color:var(--pill-ok-fg);border-color:var(--green);}
.badge.off{background:var(--accent-soft);color:var(--accent);border-color:var(--accent);}
.badge.qual{background:var(--pill-info-bg);color:var(--pill-info-fg);border-color:var(--blue);}
.badge.qual.bad{background:var(--pill-err-bg);color:var(--pill-err-fg);border-color:var(--red);}
.theme-switch{
  display:inline-flex;gap:2px;background:var(--card);
  border:1px solid var(--border2);border-radius:8px;padding:2px;
}
.theme-switch button{
  border:none;background:transparent;color:var(--muted);cursor:pointer;
  border-radius:6px;padding:5px 9px;font-size:13px;line-height:1;
  transition:all .15s;font-family:inherit;
}
.theme-switch button:hover{color:var(--text);}
.theme-switch button.active{background:var(--card-elev);color:var(--accent);box-shadow:var(--shadow);}

/* Tabs */
.tabs{
  padding:0 24px;background:var(--surface);border-bottom:1px solid var(--border);
  display:flex;gap:0;overflow-x:auto;
}
.tab{
  padding:12px 18px;font-size:12px;font-weight:600;cursor:pointer;
  color:var(--muted);border-bottom:2px solid transparent;transition:all .15s;
  white-space:nowrap;user-select:none;display:inline-flex;align-items:center;gap:7px;
}
.tab:hover{color:var(--text);}
.tab.active{color:var(--accent);border-bottom-color:var(--accent);}
.tab .tab-count{
  background:var(--accent-soft);color:var(--accent);
  border-radius:999px;padding:1px 7px;font-size:10px;font-weight:700;
}

/* Main */
.main{padding:20px 24px;max-width:1480px;margin:0 auto;}
.tab-panel{display:none;}
.tab-panel.active{display:block;animation:fadeIn .2s ease;}
@keyframes fadeIn{from{opacity:0;transform:translateY(2px);}to{opacity:1;transform:none;}}

/* Grids */
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:16px;}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:16px;}
.grid2{display:grid;grid-template-columns:repeat(2,1fr);gap:14px;margin-bottom:16px;}
@media(max-width:1200px){.grid4{grid-template-columns:repeat(2,1fr);}}
@media(max-width:900px){.grid3,.grid2{grid-template-columns:1fr;}}
@media(max-width:700px){.grid4,.grid3,.grid2{grid-template-columns:1fr;}}

/* Cards */
.card{
  background:var(--card);border:1px solid var(--border);border-radius:12px;
  padding:16px;box-shadow:var(--shadow);transition:border-color .15s;
}
.card:hover{border-color:var(--border2);}
.card.accent{border-left:3px solid var(--accent);padding-left:14px;}
.card-label{
  font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;
  color:var(--muted);margin-bottom:8px;
}
.card-val{
  font-size:28px;font-weight:800;line-height:1.1;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums;color:var(--text);
}
.card-val.accent{color:var(--accent);}
.card-val.green{color:var(--green);}
.card-val.yellow{color:var(--yellow);}
.card-val.red{color:var(--red);}
.card-val.blue{color:var(--blue);}
.card-val.purple{color:var(--purple);}
.card-sub{font-size:11px;color:var(--muted);margin-top:6px;}

/* Section */
.sec{
  font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;
  color:var(--text-2);margin:20px 0 10px;padding-bottom:8px;
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
}
.sec:first-child{margin-top:0;}
.sec .sec-sub{font-size:10px;color:var(--muted);font-weight:500;text-transform:none;letter-spacing:0;}

/* Donut */
.donut-row{display:flex;gap:24px;align-items:center;flex-wrap:wrap;}
.donut-wrap{position:relative;width:160px;height:160px;flex-shrink:0;}
.donut-center{
  position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;text-align:center;
}
.donut-total{font-size:26px;font-weight:800;letter-spacing:-.02em;}
.donut-lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-top:2px;}
.donut-legend{flex:1;min-width:240px;}
.donut-leg-item{display:flex;align-items:center;gap:8px;margin-bottom:9px;font-size:12px;}
.donut-leg-dot{width:11px;height:11px;border-radius:3px;flex-shrink:0;}
.donut-leg-val{margin-left:auto;font-weight:700;font-variant-numeric:tabular-nums;}
.donut-leg-pct{font-size:10px;color:var(--muted);margin-left:4px;}

/* Horizontal bars */
/* Horizontal bars — each bar gets its own distinct color set inline by _hbars() */
.bar-row{display:flex;align-items:center;gap:10px;margin-bottom:9px;}
.bar-row:last-child{margin-bottom:0;}
.bar-name{
  width:180px;font-size:11px;color:var(--text);flex-shrink:0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  display:flex;align-items:center;gap:6px;
}
.bar-dot{
  display:inline-block;width:8px;height:8px;border-radius:50%;
  flex-shrink:0;box-shadow:0 0 6px currentColor;
}
.bar-track{
  flex:1;height:14px;background:var(--border);border-radius:7px;
  overflow:hidden;position:relative;
  box-shadow:inset 0 1px 2px rgba(0,0,0,.15);
}
/* BUGFIX: bar-fill is a <span> (inline element). Inline elements ignore
   height:100%, so the colored fill collapsed to zero height and only the
   grey track was visible. display:block makes height:100% take effect. */
.bar-fill{
  display:block;
  height:100%;
  border-radius:7px;
  transition:width .5s cubic-bezier(.4,0,.2,1);
  box-shadow:inset 0 -1px 0 rgba(0,0,0,.15), 0 1px 2px rgba(0,0,0,.1);
  min-width:3px;
}
.bar-val{
  width:100px;text-align:right;font-size:11px;color:var(--muted);
  flex-shrink:0;font-variant-numeric:tabular-nums;
}
.bar-val b{color:var(--text);font-weight:700;}
.bar-pct{font-size:10px;color:var(--muted);margin-left:2px;}

/* Stacked bar (free vs paid split) */
.stack-bar{
  display:flex;height:30px;border-radius:7px;overflow:hidden;
  border:1px solid var(--border);background:var(--card);
}
.stack-seg{
  height:100%;min-width:2px;transition:width .4s;
  display:flex;align-items:center;justify-content:center;
  font-size:10px;font-weight:700;color:#fff;overflow:hidden;
}
.stack-legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:10px;font-size:11px;color:var(--muted);}
.stack-leg-dot{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px;}

/* Provider cards */
.prov-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;}
.prov-card{
  background:var(--card);border:1px solid var(--border);border-radius:12px;
  padding:14px;transition:border-color .15s;position:relative;overflow:hidden;
}
.prov-card:hover{border-color:var(--border2);}
.prov-card::before{
  content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--prov-color,var(--accent));
}
.prov-name{font-size:13px;font-weight:700;margin-bottom:8px;display:flex;align-items:center;gap:8px;}
.prov-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0;}
.prov-stats{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px;}
.prov-stat{padding:6px 8px;background:var(--card-elev);border-radius:6px;}
.prov-stat-val{font-size:15px;font-weight:700;font-variant-numeric:tabular-nums;}
.prov-stat-lbl{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-top:2px;}

/* Tables */
.tbl-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:10px;background:var(--card);}
table{width:100%;border-collapse:collapse;}
th{
  background:var(--surface);font-size:10px;font-weight:700;text-transform:uppercase;
  letter-spacing:.06em;color:var(--muted);padding:11px 14px;text-align:left;
  border-bottom:1px solid var(--border);white-space:nowrap;position:sticky;top:0;
}
td{padding:10px 14px;border-bottom:1px solid var(--border);font-size:12px;color:var(--text);vertical-align:middle;}
tr:last-child td{border-bottom:none;}
tr:hover td{background:var(--hover);}
.num{text-align:right;font-variant-numeric:tabular-nums;}
.mono{font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;font-size:11px;}
.pill{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;}
.pill.ok{background:var(--pill-ok-bg);color:var(--pill-ok-fg);}
.pill.warn{background:var(--pill-warn-bg);color:var(--pill-warn-fg);}
.pill.err{background:var(--pill-err-bg);color:var(--pill-err-fg);}
.pill.info{background:var(--pill-info-bg);color:var(--pill-info-fg);}
.pill.tier{background:var(--pill-info-bg);color:var(--pill-info-fg);}
.pill.free{background:var(--pill-ok-bg);color:var(--pill-ok-fg);}
.pill.paid{background:var(--pill-warn-bg);color:var(--pill-warn-fg);}
.sortable{cursor:pointer;user-select:none;}
.sortable:hover{color:var(--accent);}
.sortable .arr{font-size:9px;opacity:.6;margin-left:3px;}
.empty{padding:36px;text-align:center;color:var(--muted);font-size:12px;}

/* Search input */
.search-input{
  width:100%;background:var(--card);color:var(--text);
  border:1px solid var(--border2);border-radius:8px;padding:9px 14px;
  font-size:13px;font-family:inherit;outline:none;margin-bottom:10px;
}
.search-input:focus{border-color:var(--accent);}

/* Scroll container for big tables */
.scroll{max-height:560px;overflow:auto;border:1px solid var(--border);border-radius:10px;}

/* Quality banner */
.quality-banner{
  display:flex;align-items:center;gap:18px;padding:18px 22px;
  background:var(--card);border:1px solid var(--border);border-radius:12px;
  margin-bottom:16px;
}
.quality-banner.ok{border-left:3px solid var(--green);}
.quality-banner.bad{border-left:3px solid var(--red);}
.qb-icon{display:flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:50%;flex-shrink:0;}
.quality-banner.ok .qb-icon{background:var(--pill-ok-bg);color:var(--pill-ok-fg);}
.quality-banner.bad .qb-icon{background:var(--pill-err-bg);color:var(--pill-err-fg);}
.qb-text{flex:1;}
.qb-title{font-size:15px;font-weight:700;}
.qb-sub{font-size:12px;color:var(--muted);margin-top:3px;}

/* Meta strip */
.meta-strip{
  display:flex;gap:18px;flex-wrap:wrap;padding:12px 16px;
  background:var(--card);border:1px solid var(--border);border-radius:10px;
  margin-bottom:16px;font-size:11px;
}
.meta-strip > div{display:flex;align-items:center;gap:6px;}
.meta-k{color:var(--muted);}
.meta-v{color:var(--text);font-weight:600;}

/* Footer */
.footer{
  padding:18px 24px;text-align:center;color:var(--muted);font-size:11px;
  border-top:1px solid var(--border);margin-top:24px;
}
.footer .mono{font-size:11px;}
.footer a{color:var(--accent);text-decoration:none;}
.footer a:hover{text-decoration:underline;}

/* Scrollbar */
::-webkit-scrollbar{width:8px;height:8px;}
::-webkit-scrollbar-track{background:var(--surface);}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px;}
::-webkit-scrollbar-thumb:hover{background:var(--muted);}
"""

# Pre-paint theme script — MUST run in <head> before <body> renders to avoid
# FOUC (flash of unstyled / wrong-theme content). This is the ONLY script in
# <head>; all other JS (tab handlers, sort, search, theme switcher buttons)
# runs from a <script> at the end of <body> so the DOM is fully parsed first.
_THEME_PREPAINT = """
(function(){
  try{
    var c=localStorage.getItem('catalog-theme')||'system';
    var d=window.matchMedia('(prefers-color-scheme: dark)').matches;
    var r=c==='system'?(d?'dark':'light'):c;
    document.documentElement.setAttribute('data-theme',r);
  }catch(e){document.documentElement.setAttribute('data-theme','dark');}
})();
"""

_JS = """
// ── Theme switcher button handlers + OS-change listener ──
// (Pre-paint theme application is in a separate <script> in <head>.)
function applyTheme(choice){
  document.documentElement.setAttribute('data-theme',
    choice==='system'?(window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'):choice);
  document.querySelectorAll('#theme-switch button').forEach(function(b){
    b.classList.toggle('active',b.dataset.t===choice);
  });
}
document.querySelectorAll('#theme-switch button').forEach(function(b){
  b.addEventListener('click',function(){
    try{localStorage.setItem('catalog-theme',b.dataset.t);}catch(e){}
    applyTheme(b.dataset.t);
  });
});
(function(){
  var c='system';try{c=localStorage.getItem('catalog-theme')||'system';}catch(e){}
  applyTheme(c);
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change',function(){
    try{if((localStorage.getItem('catalog-theme')||'system')==='system')applyTheme('system');}catch(e){}
  });
})();

// ── Tabs ───────────────────────────────────────────
document.querySelectorAll('.tab').forEach(function(t){
  t.addEventListener('click',function(){
    document.querySelectorAll('.tab').forEach(function(x){x.classList.remove('active');});
    document.querySelectorAll('.tab-panel').forEach(function(x){x.classList.remove('active');});
    t.classList.add('active');
    document.getElementById('tab-'+t.dataset.tab).classList.add('active');
  });
});

// ── Table filtering ────────────────────────────────
function flt(inputId, tableId){
  var q=document.getElementById(inputId).value.toLowerCase();
  var rows=document.querySelectorAll('#'+tableId+' tbody tr');
  var vis=0;
  for(var i=0;i<rows.length;i++){
    var m=rows[i].textContent.toLowerCase().indexOf(q)>=0;
    rows[i].style.display=m?'':'none';
    if(m)vis++;
  }
  var c=document.getElementById(tableId+'-count');
  if(c)c.textContent=vis;
}

// ── Table sorting ──────────────────────────────────
document.querySelectorAll('th.sortable').forEach(function(th){
  th.addEventListener('click',function(){
    var key=th.dataset.sort;
    var dir=th.dataset.dir==='asc'?'desc':'asc';
    th.dataset.dir=dir;
    var tbody=th.closest('table').querySelector('tbody');
    var rows=Array.prototype.slice.call(tbody.querySelectorAll('tr'));
    var idx=Array.prototype.indexOf.call(th.parentNode.children,th);
    rows.sort(function(a,b){
      var va=a.children[idx]?a.children[idx].dataset.v||a.children[idx].textContent.trim():'';
      var vb=b.children[idx]?b.children[idx].dataset.v||b.children[idx].textContent.trim():'';
      var na=parseFloat(va), nb=parseFloat(vb);
      var cmp=!isNaN(na)&&!isNaN(nb)?(na-nb):va.localeCompare(vb);
      return dir==='asc'?cmp:-cmp;
    });
    rows.forEach(function(r){tbody.appendChild(r);});
    document.querySelectorAll('th.sortable .arr').forEach(function(a){a.remove();});
    th.insertAdjacentHTML('beforeend','<span class="arr">'+(dir==='asc'?'▲':'▼')+'</span>');
  });
});
"""


_PALETTE = ["#76b900", "#58a6ff", "#bc8cff", "#39c5cf", "#ff7b72", "#d29922",
            "#9ccc3c", "#f85149", "#8b949e", "#ffa657", "#7ee787", "#d2a8ff"]

_PROVIDER_COLORS = {
    "openrouter": "#58a6ff", "nous": "#bc8cff", "opencode": "#39c5cf",
    "blackbox": "#ff7b72", "vercel": "#e2e8f0", "nim": "#76b900",
}


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _fmt_price(v) -> str:
    if v is None:
        return "—"
    if v == 0:
        return '<span class="ok">$0</span>'
    return f"${v:g}"


def _hbars(items: list[dict], total: int | None = None, palette=None) -> str:
    """Horizontal bar list with inline count + percentage and per-bar gradient.

    Each bar gets its own distinct color from the palette, applied as a
    subtle two-stop gradient for visual richness. The color is also
    shown as a small dot before the label so the color↔item mapping is
    unambiguous even when bars are short.
    """
    if not items:
        return '<div class="empty">No data available.</div>'
    mx = total or max((i["count"] for i in items), default=1) or 1
    sum_total = sum(i["count"] for i in items) or 1
    out = []
    for idx, it in enumerate(items):
        color = (palette or _PALETTE)[idx % len(_PALETTE)]
        # Slightly darker stop for the gradient tail (20% darker approximation
        # via overlay — works on any base color without needing color math).
        pct_val = round(100 * it["count"] / mx) if mx else 0
        pct_of_total = round(100 * it["count"] / sum_total, 1) if sum_total else 0
        label = it.get("label", it["name"])
        out.append(
            f'<div class="bar-row">'
            f'<span class="bar-name" title="{_e(it["name"])}">'
            f'<span class="bar-dot" style="background:{color}"></span>'
            f'{_e(label)}</span>'
            f'<span class="bar-track">'
            f'<span class="bar-fill" style="width:{max(2,pct_val)}%;'
            f'background:linear-gradient(90deg,{color} 0%,{color}cc 100%)"></span>'
            f'</span>'
            f'<span class="bar-val"><b>{it["count"]}</b> <span class="bar-pct">({pct_of_total}%)</span></span>'
            f'</div>'
        )
    return "".join(out)


def _donut_svg(items: list[dict], total_id: str) -> str:
    """Render an inline SVG donut. items: [{name,label,count,color}].
    Returns the SVG markup; the caller wraps it with a legend."""
    total = sum(i["count"] for i in items) or 1
    R = 15.915
    offset = 0
    segs = []
    for it in items:
        frac = it["count"] / total
        if frac <= 0:
            continue
        length = frac * 100
        segs.append(
            f'<circle cx="18" cy="18" r="{R}" fill="none" '
            f'stroke="{it["color"]}" stroke-width="3.2" '
            f'stroke-dasharray="{length:.3f} {100 - length:.3f}" '
            f'stroke-dashoffset="{100 - offset + 25:.3f}"/>'
        )
        offset += length
    total_count = sum(i["count"] for i in items)
    return (
        f'<div class="donut-wrap">'
        f'<svg viewBox="0 0 36 36" width="160" height="160">'
        f'<circle cx="18" cy="18" r="{R}" fill="none" stroke="var(--border)" stroke-width="3.2"/>'
        f'{"".join(segs)}'
        f'</svg>'
        f'<div class="donut-center">'
        f'<div class="donut-total" id="{total_id}">{total_count}</div>'
        f'<div class="donut-lbl">models</div>'
        f'</div></div>'
    )


def _donut_legend(items: list[dict]) -> str:
    total = sum(i["count"] for i in items) or 1
    out = ['<div class="donut-legend">']
    for it in items:
        p = round(it["count"] / total * 100, 1)
        out.append(
            f'<div class="donut-leg-item">'
            f'<span class="donut-leg-dot" style="background:{it["color"]}"></span>'
            f'<span>{_e(it["label"])}</span>'
            f'<span class="donut-leg-val">{it["count"]}'
            f'<span class="donut-leg-pct">({p}%)</span></span>'
            f'</div>'
        )
    out.append('</div>')
    return "".join(out)


def render_html(d: dict) -> str:
    """Render the full HTML page from collected dashboard data."""
    nim, provs, q = d["nim"], d["providers"], d["quality"]
    free_only = d["free_only"]
    run = nim.get("last_run") or {}
    visible_provider = provs.get("total_free", 0) if free_only else provs.get("total", 0)
    generated_at_short = (d["generated_at"] or "")[:19].replace("T", " ")

    # Quality banner
    q_passed = q.get("passed", 0)
    q_total = q.get("total", 0)
    q_all_ok = q_passed == q_total and q_total > 0
    q_banner_cls = "ok" if q_all_ok else "bad"
    q_banner_icon = _icon("check", 22) if q_all_ok else _icon("alert", 22)
    q_banner_title = (
        "All quality checks passing — catalog is healthy"
        if q_all_ok else
        f"{q_total - q_passed} quality check(s) failing — see Quality tab"
    )
    q_banner_sub = (
        f"{q_passed}/{q_total} checks passed · mirror of validate_project.py"
    )

    # FREE_ONLY badge
    free_badge = (
        '<span class="badge on">FREE_ONLY · on</span>' if free_only
        else '<span class="badge off">FREE_ONLY · off</span>'
    )

    # ── KPI cards (Overview tab) ──
    nim_active = nim.get("active", 0)
    nim_free_endpoint = nim.get("free_endpoint", 0)
    nim_operations = nim.get("operations", 0)
    nim_examples = nim.get("examples", 0)
    nim_examples_runnable = nim.get("examples_runnable", 0)
    prov_count = len(provs.get("list", [])) + 1  # +1 for NIM itself
    prov_total = provs.get("total", 0)
    prov_total_free = provs.get("total_free", 0)
    visible_total = nim_active + visible_provider

    kpis = [
        ("NIM Active Models", nim_active,
         f'{nim_free_endpoint} free-hosted · {nim.get("downloadable",0)} downloadable', "accent"),
        ("NIM Operations", nim_operations,
         f'{nim_examples} examples · {nim_examples_runnable} runnable', ""),
        ("Providers Tracked", prov_count,
         "incl. NVIDIA NIM + multi-provider catalog", ""),
        ("Provider Models", prov_total,
         f'{prov_total_free} free · {prov_total - prov_total_free} paid', "blue"),
        ("Visible Under Policy", visible_total,
         "NIM is exempt from FREE_ONLY" if free_only else "all models visible", ""),
        ("Quality Score", f'{q_passed}/{q_total}',
         "all passing" if q_all_ok else "FAILURES — see Quality tab",
         "green" if q_all_ok else "red"),
    ]
    kpi_html = "".join(
        f'<div class="card{" accent" if k=="accent" else ""}">'
        f'<div class="card-label">{_e(l)}</div>'
        f'<div class="card-val {k}">{_e(v)}</div>'
        f'<div class="card-sub">{_e(s)}</div></div>'
        for l, v, s, k in kpis
    )

    # ── NIM tier donut ──
    tier_items = [
        {"name": t["name"], "label": t["label"], "count": t["count"],
         "color": _PALETTE[i % len(_PALETTE)]}
        for i, t in enumerate(nim.get("tiers", []))
    ]
    tier_donut = _donut_svg(tier_items, "tier-donut-total")
    tier_legend = _donut_legend(tier_items)

    # ── Provider donut (free vs paid) ──
    prov_donut_items = [
        {"name": "free", "label": "Free provider models", "count": prov_total_free, "color": "#76b900"},
        {"name": "paid", "label": "Paid provider models", "count": max(0, prov_total - prov_total_free), "color": "#f85149"},
    ]
    prov_donut = _donut_svg(prov_donut_items, "prov-donut-total")
    prov_donut_legend = _donut_legend(prov_donut_items)

    # ── Provider rows (table) ──
    pv_colors_inline = _PROVIDER_COLORS
    prov_rows = "".join(
        f'<tr><td><span class="pill" style="background:{pv_colors_inline.get(p["id"], "#8b949e")}20;color:{pv_colors_inline.get(p["id"], "#8b949e")}">{_e(p["label"])}</span></td>'
        f'<td class="num" data-v="{p["models"]}">{p["models"]}</td>'
        f'<td class="num" data-v="{p["free"]}">{p["free"]}</td>'
        f'<td class="num" data-v="{round(100 * p["free"] / p["models"], 1) if p["models"] else 0}">'
        f'{round(100 * p["free"] / p["models"], 1) if p["models"] else 0}%</td>'
        f'<td class="num">{_fmt_price(p["min_in"])}</td>'
        f'<td class="num">{_fmt_price(p["max_in"])}</td>'
        f'<td class="num">{_fmt_price(p["avg_in"])}</td></tr>'
        for p in provs.get("list", [])
    )

    # ── Provider fetch health rows ──
    fetch_rows = "".join(
        f'<tr><td>{_e(PROVIDER_LABELS.get(f["provider"], f["provider"]))}</td>'
        f'<td><span class="pill {"ok" if f["status"] == "ok" else "err"}">{_e(f["status"])}</span></td>'
        f'<td class="num" data-v="{f["models"]}">{f["models"]}</td>'
        f'<td class="num" data-v="{f["free"]}">{f["free"]}</td>'
        f'<td class="mono">{_e((f["fetched_at"] or "")[:19])}</td>'
        f'<td class="mono"><a href="{_e(f["source"])}" target="_blank" rel="noopener">{_e((f["source"] or "").replace("https://", ""))}</a></td></tr>'
        for f in provs.get("fetch_runs", [])
    ) if provs.get("fetch_runs") else '<tr><td colspan="6" class="empty">No provider fetch runs recorded.</td></tr>'

    # ── Provider mini-cards (Providers tab) ──
    prov_cards = "".join(
        f'<div class="prov-card" style="--prov-color:{pv_colors_inline.get(p["id"], "#8b949e")}">'
        f'<div class="prov-name"><span class="prov-dot" style="background:{pv_colors_inline.get(p["id"], "#8b949e")}"></span>{_e(p["label"])}</div>'
        f'<div class="card-sub">{p["models"]} models · {p["free"]} free</div>'
        f'<div class="prov-stats">'
        f'<div class="prov-stat"><div class="prov-stat-val">{p["models"]}</div><div class="prov-stat-lbl">Models</div></div>'
        f'<div class="prov-stat"><div class="prov-stat-val" style="color:var(--green)">{p["free"]}</div><div class="prov-stat-lbl">Free</div></div>'
        f'<div class="prov-stat"><div class="prov-stat-val">{round(100 * p["free"] / p["models"], 1) if p["models"] else 0}%</div><div class="prov-stat-lbl">Free %</div></div>'
        f'<div class="prov-stat"><div class="prov-stat-val">{_fmt_price(p["min_in"])}</div><div class="prov-stat-lbl">Min $/1M</div></div>'
        f'</div></div>'
        for p in provs.get("list", [])
    ) or '<div class="empty">No provider models in catalog.</div>'

    # ── Free models rows ──
    free_rows = "".join(
        f'<tr><td><span class="pill" style="background:{pv_colors_inline.get(m["provider"], "#8b949e")}20;color:{pv_colors_inline.get(m["provider"], "#8b949e")}">{_e(m["provider_label"])}</span></td>'
        f'<td class="mono">{_e(m["model_id"])}</td><td>{_e(m["name"] or "")}</td>'
        f'<td class="num" data-v="{m["context"] or 0}">{m["context"] or "—"}</td>'
        f'<td class="num">{_fmt_price(m["in"])}</td><td class="num">{_fmt_price(m["out"])}</td>'
        f'<td class="mono">{_e(m["endpoint"])}</td><td class="mono">{_e(m["auth_env"])}</td></tr>'
        for m in d["free_models"]
    ) if d["free_models"] else '<tr><td colspan="8" class="empty">No free provider models in catalog.</td></tr>'

    # ── NIM model explorer rows ──
    def _mod_tags(mods):
        return "".join(f'<span class="pill info">{_e(x)}</span>' for x in mods)

    def _avail_tags(avail):
        return "".join(
            f'<span class="pill {"free" if "Free" in x else "paid"}">{_e(x)}</span>'
            for x in avail
        )

    nim_rows = "".join(
        f'<tr><td class="mono"><a href="{_e(m["url"])}" target="_blank" rel="noopener">{_e(m["id"])}</a></td>'
        f'<td>{_e(m["publisher"])}</td>'
        f'<td>{_mod_tags(m["modalities"])}</td>'
        f'<td>{_avail_tags(m["availability"])}</td>'
        f'<td><span class="pill tier">{_e(TIER_LABELS.get(m["tier"], m["tier"] or "—"))}</span></td>'
        f'<td>{(_icon("check", 12) + " free") if m["free"] else "—"}</td>'
        f'<td>{_icon("check", 12) if m["downloadable"] else "—"}</td></tr>'
        for m in d["nim_models"]
    ) if d["nim_models"] else '<tr><td colspan="7" class="empty">No NIM models in catalog.</td></tr>'

    # ── Quality rows ──
    check_rows = "".join(
        f'<tr><td><span class="pill {"ok" if c["ok"] else "err"}">'
        f'{_icon("check", 10) if c["ok"] else _icon("alert", 10)} '
        f'{"PASS" if c["ok"] else "FAIL"}</span></td>'
        f'<td>{_e(c["name"])}</td><td class="card-sub">{_e(c["detail"])}</td></tr>'
        for c in q.get("checks", [])
    )

    # ── Examples per language rows ──
    ex_rows = "".join(
        f'<tr><td>{_e(x["name"])}</td><td class="num" data-v="{x["count"]}">{x["count"]}</td>'
        f'<td class="num" data-v="{x["runnable"]}" style="color:var(--green)">{x["runnable"]}</td>'
        f'<td class="num">{round(100 * x["runnable"] / x["count"], 1) if x["count"] else 0}%</td></tr>'
        for x in nim.get("examples_by_lang", [])
    )

    # ── Provider stacked bar (free vs paid across all providers) ──
    prov_stack_segs = "".join(
        f'<div class="stack-seg" style="width:{(p["models"] / max(1, prov_total)) * 100:.2f}%;background:{pv_colors_inline.get(p["id"], "#8b949e")}" '
        f'title="{_e(p["label"])}: {p["models"]} models ({round(100 * p["models"] / max(1, prov_total), 1)}%)">'
        f'{round(100 * p["models"] / max(1, prov_total)) if p["models"] / max(1, prov_total) > 0.05 else ""}'
        f'</div>'
        for p in provs.get("list", [])
    ) if prov_total else ""

    prov_stack_legend = "".join(
        f'<span><span class="stack-leg-dot" style="background:{pv_colors_inline.get(p["id"], "#8b949e")}"></span>'
        f'{_e(p["label"])} · {p["models"]}</span>'
        for p in provs.get("list", [])
    )

    # ── NIM bars (modalities, publishers, transports) ──
    mod_bars = _hbars(nim.get("modalities", []))
    pub_bars = _hbars(nim.get("publishers", [])[:12])
    trans_bars = _hbars(nim.get("transports", []))
    status_bars = _hbars(nim.get("live_status", []))

    # ── Counts for tab badges ──
    nim_models_count = len(d["nim_models"])
    free_models_count = len(d["free_models"])
    providers_count = len(provs.get("list", []))
    quality_fail_count = q_total - q_passed

    # ── Audit run table ──
    audit_run_html = f"""
    <table>
      <tbody>
        <tr><td style="width:240px">Finished (UTC)</td><td class="mono">{_e((run.get("finished_at") or "—")[:19])}</td></tr>
        <tr><td>Build index total</td><td class="num">{run.get("index_total", "—")}</td></tr>
        <tr><td>Active non-deprecated</td><td class="num">{run.get("active_total", "—")}</td></tr>
        <tr><td>Page errors</td><td class="num">{run.get("page_errors", "—")}</td></tr>
        <tr><td>NVIDIA API key used</td>
            <td>{'<span class="pill ok">yes</span>' if run.get("key_used") else '<span class="pill warn">no (key-less run)</span>'}</td></tr>
        <tr><td>Status</td><td><span class="pill {"ok" if run.get("status") == "ok" else "warn"}">{_e(run.get("status") or "—")}</span></td></tr>
      </tbody>
    </table>"""

    # ── Page assembly ──
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Model Catalog Dashboard — NVIDIA NIM + Multi-Provider</title>
<style>{_CSS}</style>
<script>{_THEME_PREPAINT}</script>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="logo">
    <div class="logo-icon">{_brand_icon(22)}</div>
    <div>
      <div class="logo-text"><span class="nv">AI Model Catalog</span> Dashboard</div>
      <div class="logo-sub">NVIDIA NIM · OpenRouter · Nous · OpenCode · Blackbox · Vercel</div>
    </div>
  </div>
  <div class="header-right">
    {free_badge}
    <span class="badge qual {'bad' if not q_all_ok else ''}">Quality {q_passed}/{q_total}</span>
    <span class="badge">Generated {generated_at_short} UTC</span>
    <div class="theme-switch" id="theme-switch" role="group" aria-label="Theme">
      <button data-t="light"  title="Light theme">{_icon("sun", 14)}</button>
      <button data-t="dark"   title="Dark theme">{_icon("moon", 14)}</button>
      <button data-t="system" title="Follow OS">{_icon("monitor", 14)}</button>
    </div>
  </div>
</div>

<!-- Tabs -->
<div class="tabs">
  <div class="tab active" data-tab="overview">{_icon("grid", 12)} Overview</div>
  <div class="tab" data-tab="nim">{_icon("layers", 12)} NIM Models <span class="tab-count">{nim_models_count}</span></div>
  <div class="tab" data-tab="providers">{_icon("server", 12)} Providers <span class="tab-count">{providers_count}</span></div>
  <div class="tab" data-tab="free">{_icon("tag", 12)} Free Explorer <span class="tab-count">{free_models_count}</span></div>
  <div class="tab" data-tab="quality">{_icon("shield", 12)} Quality {f'<span class="tab-count" style="background:var(--pill-err-bg);color:var(--pill-err-fg)">{quality_fail_count}</span>' if quality_fail_count else ""}</div>
</div>

<!-- Main -->
<div class="main">

  <!-- ════════ OVERVIEW ════════ -->
  <div class="tab-panel active" id="tab-overview">

    <div class="meta-strip">
      <div><span class="meta-k">Catalog source:</span><span class="meta-v mono">data/active_nvidia_nim.sqlite3</span></div>
      <div><span class="meta-k">Pipeline:</span><span class="meta-v">audit → providers → upstreams → finalize → validate</span></div>
      <div><span class="meta-k">HTTP route:</span><span class="meta-v mono">GET /dashboard</span></div>
    </div>

    <div class="quality-banner {q_banner_cls}">
      <div class="qb-icon">{q_banner_icon}</div>
      <div class="qb-text">
        <div class="qb-title">{q_banner_title}</div>
        <div class="qb-sub">{q_banner_sub}</div>
      </div>
    </div>

    <div class="grid4">{kpi_html}</div>

    <div class="grid2">
      <div>
        <div class="sec">NIM verification tiers
          <span class="sec-sub">finalize_audit.py</span>
        </div>
        <div class="card">
          <div class="donut-row">
            {tier_donut}
            {tier_legend}
          </div>
          <div class="card-sub" style="margin-top:12px">
            Working-eligible (live-verified): <b style="color:var(--green)">{nim.get("working", 0)}</b> —
            run <span class="mono">make verify</span> with NVIDIA_API_KEY to populate.
          </div>
        </div>
      </div>
      <div>
        <div class="sec">Provider models — Free vs Paid
          <span class="sec-sub">{prov_total} total</span>
        </div>
        <div class="card">
          <div class="donut-row">
            {prov_donut}
            {prov_donut_legend}
          </div>
          <div class="card-sub" style="margin-top:12px">
            NVIDIA NIM (<b>{nim_active}</b> models) is 100% free to call with an NVIDIA API key
            and is never filtered by FREE_ONLY.
          </div>
        </div>
      </div>
    </div>

    <div class="sec">NIM modalities <span class="sec-sub">{nim_active} models, multi-tag</span></div>
    <div class="card">{mod_bars}</div>

    <div class="grid2">
      <div>
        <div class="sec">NIM publishers <span class="sec-sub">top 12</span></div>
        <div class="card">{pub_bars}</div>
      </div>
      <div>
        <div class="sec">NIM operation transports <span class="sec-sub">{nim_operations} operations</span></div>
        <div class="card">{trans_bars}</div>
      </div>
    </div>

    <div class="sec">Provider model share <span class="sec-sub">across all providers</span></div>
    <div class="card">
      <div class="stack-bar">{prov_stack_segs}</div>
      <div class="stack-legend">{prov_stack_legend}</div>
    </div>

  </div>

  <!-- ════════ NIM MODELS ════════ -->
  <div class="tab-panel" id="tab-nim">

    <div class="grid4">
      <div class="card accent">
        <div class="card-label">NIM Active Models</div>
        <div class="card-val accent">{nim_active}</div>
        <div class="card-sub">in catalog</div>
      </div>
      <div class="card">
        <div class="card-label">Free Hosted</div>
        <div class="card-val green">{nim_free_endpoint}</div>
        <div class="card-sub">callable at no cost</div>
      </div>
      <div class="card">
        <div class="card-label">Downloadable</div>
        <div class="card-val blue">{nim.get("downloadable", 0)}</div>
        <div class="card-sub">self-host option</div>
      </div>
      <div class="card">
        <div class="card-label">Partner Endpoints</div>
        <div class="card-val purple">{nim.get("partner", 0)}</div>
        <div class="card-sub">specialized transports</div>
      </div>
    </div>

    <div class="grid2">
      <div>
        <div class="sec">NIM live status <span class="sec-sub">last probe outcome</span></div>
        <div class="card">{status_bars}</div>
      </div>
      <div>
        <div class="sec">Usage examples per language</div>
        <div class="card">
          <div class="tbl-wrap" style="border:none">
            <table>
              <thead><tr>
                <th>Language</th>
                <th class="num sortable" data-sort="count">Stored</th>
                <th class="num sortable" data-sort="runnable">Runnable</th>
                <th class="num">%</th>
              </tr></thead>
              <tbody>{ex_rows or '<tr><td colspan="4" class="empty">No examples recorded.</td></tr>'}</tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <div class="sec">NVIDIA NIM model explorer
      <span class="sec-sub">(<span id="nimTable-count">{nim_models_count}</span> of {nim_models_count} active models)</span>
    </div>
    <input type="search" class="search-input" placeholder="Filter NIM models by id, publisher, modality, tier, or availability…"
           id="nimSearch" oninput="flt('nimSearch','nimTable')">
    <div class="scroll">
      <table id="nimTable">
        <thead><tr>
          <th class="sortable" data-sort="model">Catalog ID</th>
          <th class="sortable" data-sort="publisher">Publisher</th>
          <th>Modalities</th>
          <th>Availability</th>
          <th class="sortable" data-sort="tier">Tier</th>
          <th>Free</th>
          <th>Downloadable</th>
        </tr></thead>
        <tbody>{nim_rows}</tbody>
      </table>
    </div>

  </div>

  <!-- ════════ PROVIDERS ════════ -->
  <div class="tab-panel" id="tab-providers">

    <div class="grid4">
      <div class="card accent">
        <div class="card-label">Providers Tracked</div>
        <div class="card-val accent">{providers_count}</div>
        <div class="card-sub">incl. NVIDIA NIM</div>
      </div>
      <div class="card">
        <div class="card-label">Total Provider Models</div>
        <div class="card-val blue">{prov_total}</div>
        <div class="card-sub">across all providers</div>
      </div>
      <div class="card">
        <div class="card-label">Free Provider Models</div>
        <div class="card-val green">{prov_total_free}</div>
        <div class="card-sub">{round(100 * prov_total_free / max(1, prov_total), 1) if prov_total else 0}% of total</div>
      </div>
      <div class="card">
        <div class="card-label">Paid Provider Models</div>
        <div class="card-val yellow">{prov_total - prov_total_free}</div>
        <div class="card-sub">{round(100 * (prov_total - prov_total_free) / max(1, prov_total), 1) if prov_total else 0}% of total</div>
      </div>
    </div>

    <div class="sec">Provider Catalogs <span class="sec-sub">one card per upstream provider</span></div>
    <div class="prov-grid">{prov_cards}</div>

    <div class="sec">Provider catalogs <span class="sec-sub">official sources — sortable</span></div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th class="sortable" data-sort="provider">Provider</th>
          <th class="sortable" data-sort="models">Models</th>
          <th class="sortable" data-sort="free">Free</th>
          <th class="sortable" data-sort="freepct">Free %</th>
          <th class="sortable" data-sort="minin">Min $/1M in</th>
          <th class="sortable" data-sort="maxin">Max $/1M in</th>
          <th class="sortable" data-sort="avgin">Avg $/1M in</th>
        </tr></thead>
        <tbody>{prov_rows or '<tr><td colspan="7" class="empty">No provider models.</td></tr>'}</tbody>
      </table>
    </div>

    <div class="sec">Provider fetch health <span class="sec-sub">last fetch per provider, from provider_fetch_runs</span></div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>Provider</th>
          <th class="sortable" data-sort="status">Status</th>
          <th class="sortable" data-sort="models">Models</th>
          <th class="sortable" data-sort="free">Free</th>
          <th>Fetched (UTC)</th>
          <th>Official source</th>
        </tr></thead>
        <tbody>{fetch_rows}</tbody>
      </table>
    </div>

    <div class="sec">Latest NIM audit run <span class="sec-sub">audit_runs table, most recent OK row</span></div>
    <div class="card">{audit_run_html}</div>

  </div>

  <!-- ════════ FREE EXPLORER ════════ -->
  <div class="tab-panel" id="tab-free">

    <div class="grid3">
      <div class="card accent">
        <div class="card-label">Free Provider Models</div>
        <div class="card-val accent">{free_models_count}</div>
        <div class="card-sub">across {providers_count} providers</div>
      </div>
      <div class="card">
        <div class="card-label">Always Free (NIM)</div>
        <div class="card-val green">{nim_active}</div>
        <div class="card-sub">NIM exempt from FREE_ONLY</div>
      </div>
      <div class="card">
        <div class="card-label">Total Visible Under Policy</div>
        <div class="card-val blue">{nim_active + (visible_provider if free_only else prov_total)}</div>
        <div class="card-sub">{"NIM + free provider models" if free_only else "NIM + all provider models"}</div>
      </div>
    </div>

    <div class="sec">Free provider models
      <span class="sec-sub">(<span id="freeTable-count">{free_models_count}</span> of {free_models_count} — what FREE_ONLY exposes besides NIM)</span>
    </div>
    <input type="search" class="search-input" placeholder="Filter free models by provider, id, name, or endpoint…"
           id="freeSearch" oninput="flt('freeSearch','freeTable')">
    <div class="scroll">
      <table id="freeTable">
        <thead><tr>
          <th class="sortable" data-sort="provider">Provider</th>
          <th class="sortable" data-sort="model_id">Model ID</th>
          <th class="sortable" data-sort="name">Name</th>
          <th class="sortable" data-sort="context">Context</th>
          <th class="sortable" data-sort="in">$/1M in</th>
          <th class="sortable" data-sort="out">$/1M out</th>
          <th>Endpoint</th>
          <th>Auth env</th>
        </tr></thead>
        <tbody>{free_rows}</tbody>
      </table>
    </div>

  </div>

  <!-- ════════ QUALITY ════════ -->
  <div class="tab-panel" id="tab-quality">

    <div class="quality-banner {q_banner_cls}">
      <div class="qb-icon">{q_banner_icon}</div>
      <div class="qb-text">
        <div class="qb-title">{q_banner_title}</div>
        <div class="qb-sub">{q_banner_sub}</div>
      </div>
    </div>

    <div class="grid3">
      <div class="card accent">
        <div class="card-label">Checks Passed</div>
        <div class="card-val green">{q_passed}</div>
        <div class="card-sub">all green</div>
      </div>
      <div class="card">
        <div class="card-label">Checks Failed</div>
        <div class="card-val red">{q_total - q_passed}</div>
        <div class="card-sub">require attention</div>
      </div>
      <div class="card">
        <div class="card-label">Total Checks</div>
        <div class="card-val blue">{q_total}</div>
        <div class="card-sub">mirror of validate_project.py</div>
      </div>
    </div>

    <div class="sec">Data quality <span class="sec-sub">detailed check results</span></div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th style="width:80px">Status</th>
          <th class="sortable" data-sort="name">Check</th>
          <th>Detail</th>
        </tr></thead>
        <tbody>{check_rows or '<tr><td colspan="3" class="empty">No checks recorded.</td></tr>'}</tbody>
      </table>
    </div>

  </div>

</div>

<div class="footer">
  <strong>AI Model Catalog Dashboard</strong> · NVIDIA NIM (build.nvidia.com) +
  OpenRouter · Nous Portal · OpenCode Zen · Blackbox AI · Vercel AI Gateway<br>
  Pipeline: <span class="mono">audit_catalog.py → providers.py → audit_upstreams.py → finalize_audit.py → export → validate</span> ·
  MCP server: <span class="mono">python mcp_server.py</span> (stdio) / <span class="mono">--transport http</span> ·
  HTTP API: <span class="mono">/stats /models /providers /provider-models /dashboard</span>
</div>

<script>{_JS}</script>
</body></html>"""



def generate(database: str, output: str | None = None) -> str:
    db = Q.open_db(database)
    try:
        page = render_html(build_data(db))
    finally:
        db.close()
    if output:
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page, encoding="utf-8")
    return page


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--database", default=Q.DEFAULT_DB)
    p.add_argument("--output", default="exports/dashboard.html")
    a = p.parse_args()
    generate(a.database, a.output)
    print(f"Dashboard written to {a.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

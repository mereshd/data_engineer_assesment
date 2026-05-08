"""Light analytics dashboard generator.

Produces a single self-contained ``analytics.html`` per run, designed to
sit alongside the machine-readable artifacts (``run_summary.json``,
``file_manifest.jsonl``, ``pii_transformations.csv``,
``pii_quarantine.csv``, ``validation_report.json``) in the
``<output>/reports/`` directory.

The dashboard has three regions:

  1. **Header + stat strip** - run id, timestamps, and color-coded
     summary tiles (files by status, total mapped replacements,
     unmapped count, validation outcome).
  2. **Entity-file network graph** - an interactive force-directed
     graph where nodes are processed files and canonical entities
     (persons, organizations, emails, phones), and edges are
     "this entity appeared in this file" weighted by occurrence count.
  3. **Quarantine panel** - unmapped values grouped by content hash,
     each with its occurrences (file + location + snippet) for
     operator triage.

The page is self-contained except for one CDN script tag for the
``vis-network`` graph library. The Python side stays stdlib-only -
no new runtime dependencies. The HTML is buildable from any
``RunResult`` so the same renderer can be reused in tests, ad-hoc
re-renders of past runs, or downstream tooling.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# Pinned to a specific minor version so the dashboard renders
# consistently. Bumping the version is a deliberate, reviewable change.
VIS_NETWORK_CDN = (
    "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"
)


def write_analytics_html(
    *,
    output_path: Path,
    summary: dict[str, Any],
    manifest: list[dict[str, Any]],
    transformations: list[dict[str, Any]],
    quarantine: list[dict[str, Any]],
) -> None:
    """Render the dashboard and write it to ``output_path``.

    All inputs are the same shapes the pipeline already builds in
    memory (the ``RunResult`` fields), so this can be called both
    from inside the pipeline and from any external code that has
    parsed the existing report artifacts.
    """
    data = _build_dashboard_data(
        summary=summary,
        manifest=manifest,
        transformations=transformations,
        quarantine=quarantine,
    )
    rendered = _render_html(data)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------- data shape


def _build_dashboard_data(
    *,
    summary: dict[str, Any],
    manifest: list[dict[str, Any]],
    transformations: list[dict[str, Any]],
    quarantine: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the JSON-serializable payload the dashboard JS reads.

    Aggregates per-file/per-entity edges so the graph collapses N
    transformations of the same entity in the same file into a
    single edge with a weight of N (rendered as edge thickness).
    """
    file_nodes: dict[str, dict[str, Any]] = {}
    for row in manifest:
        if row["status"] != "processed":
            continue
        rel = row["relative_path"]
        replacements_total = sum(row["replacements"].values())
        unmapped_total = sum(row["unmapped"].values())
        file_nodes[rel] = {
            "id": f"file:{rel}",
            "label": rel,
            "group": "file",
            "title": (
                f"{rel}\n"
                f"{row['records_processed']} records · "
                f"{replacements_total} replacements · "
                f"{unmapped_total} unmapped"
            ),
            # node size scales with replacement count so heavy files
            # are visually obvious
            "value": max(1, replacements_total),
        }

    entity_nodes: dict[str, dict[str, Any]] = {}
    edge_counts: dict[tuple[str, str], int] = {}

    for r in transformations:
        kind = r["kind"]
        token = r["token"]
        entity_id = f"{kind}:{token}"

        if entity_id not in entity_nodes:
            entity_nodes[entity_id] = {
                "id": entity_id,
                "label": token,
                "group": kind,
                "title": f"{kind}: {token}\nraw: {r['value']}",
            }

        edge_key = (f"file:{r['file']}", entity_id)
        edge_counts[edge_key] = edge_counts.get(edge_key, 0) + 1

    edges = [
        {
            "from": src,
            "to": dst,
            "value": count,
            "title": f"{count} occurrence{'s' if count != 1 else ''}",
        }
        for (src, dst), count in edge_counts.items()
    ]

    # Group quarantine occurrences by (kind, value, hash) for the
    # operator-facing panel: "this same vendor email shows up in N
    # places" is exactly what the operator wants to see.
    quar_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for r in quarantine:
        key = (r["kind"], r["value"], r["value_hash"])
        quar_groups.setdefault(key, []).append(
            {
                "file": r["file"],
                "location": r["location"],
                "snippet": r["snippet"],
            }
        )

    quarantine_data = sorted(
        [
            {
                "kind": kind,
                "value": value,
                "value_hash": vh,
                "count": len(occurrences),
                "occurrences": occurrences,
            }
            for (kind, value, vh), occurrences in quar_groups.items()
        ],
        key=lambda x: (-x["count"], x["kind"], x["value"]),
    )

    return {
        "summary": summary,
        "graph": {
            "nodes": list(file_nodes.values()) + list(entity_nodes.values()),
            "edges": edges,
        },
        "quarantine": quarantine_data,
    }


# ---------------------------------------------------------------- rendering


def _render_html(data: dict[str, Any]) -> str:
    """Embed the data payload into the HTML template safely.

    Replacing ``<`` with its unicode escape ``\\u003c`` in the JSON
    payload prevents any user-controlled string (e.g. a snippet
    containing ``</script>``) from breaking the surrounding HTML.
    JSON.parse on the JS side handles ``\\u003c`` natively.
    """
    json_payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    return _HTML_TEMPLATE.replace("__DASHBOARD_DATA__", json_payload)


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Sanitizer Run Analytics</title>
  <script src="__VIS_NETWORK_CDN__"></script>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      background: #f8fafc;
      color: #0f172a;
    }
    header {
      background: white;
      padding: 1.25rem 2rem 0.75rem;
      border-bottom: 1px solid #e2e8f0;
    }
    header h1 {
      margin: 0 0 0.25rem;
      font-size: 1.4rem;
      font-weight: 600;
    }
    header .meta {
      color: #64748b;
      font-size: 0.85rem;
      font-family: ui-monospace, "SF Mono", Menlo, monospace;
    }
    header a { color: #2563eb; text-decoration: none; margin-left: 0.5rem; }
    header a:hover { text-decoration: underline; }

    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 0.85rem;
      padding: 1rem 2rem;
      background: white;
      border-bottom: 1px solid #e2e8f0;
    }
    .stat {
      padding: 0.7rem 1rem;
      border-radius: 8px;
      background: #f1f5f9;
      border: 1px solid #e2e8f0;
    }
    .stat .label {
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #64748b;
      margin-bottom: 0.25rem;
    }
    .stat .value { font-size: 1.5rem; font-weight: 600; line-height: 1.1; }
    .stat.warn { background: #fef3c7; border-color: #fcd34d; }
    .stat.fail { background: #fee2e2; border-color: #fca5a5; }
    .stat.success { background: #dcfce7; border-color: #86efac; }

    main {
      display: grid;
      grid-template-columns: 1fr 380px;
      gap: 1rem;
      padding: 1rem 2rem 2rem;
    }
    @media (max-width: 1100px) { main { grid-template-columns: 1fr; } }

    .panel {
      background: white;
      border: 1px solid #e2e8f0;
      border-radius: 12px;
      padding: 1.25rem;
    }
    .panel h2 {
      margin: 0 0 0.75rem;
      font-size: 0.85rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #64748b;
      font-weight: 600;
    }

    #network {
      height: 600px;
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      background: #fafbfc;
    }

    .legend {
      display: flex;
      gap: 1rem;
      flex-wrap: wrap;
      margin-top: 0.85rem;
      font-size: 0.78rem;
      color: #475569;
    }
    .legend-item { display: flex; align-items: center; gap: 0.4rem; }
    .legend-dot {
      width: 14px;
      height: 14px;
      border-radius: 50%;
      display: inline-block;
      flex-shrink: 0;
    }
    .legend-dot.box { border-radius: 3px; }

    .quar-list { max-height: 600px; overflow-y: auto; padding-right: 0.25rem; }
    .quar-item {
      padding: 0.7rem 0.85rem;
      margin-bottom: 0.6rem;
      border: 1px solid #fca5a5;
      background: #fef2f2;
      border-radius: 8px;
    }
    .quar-item.empty {
      border-color: #86efac;
      background: #f0fdf4;
      color: #14532d;
      text-align: center;
      padding: 1.5rem 0.85rem;
    }
    .quar-header {
      font-family: ui-monospace, "SF Mono", Menlo, monospace;
      font-size: 0.85rem;
      font-weight: 600;
      margin-bottom: 0.3rem;
      word-break: break-all;
      line-height: 1.3;
    }
    .quar-kind {
      display: inline-block;
      padding: 0.08rem 0.4rem;
      border-radius: 4px;
      background: #fecaca;
      color: #7f1d1d;
      font-size: 0.7rem;
      text-transform: uppercase;
      margin-right: 0.5rem;
      letter-spacing: 0.04em;
    }
    .quar-meta {
      font-size: 0.7rem;
      color: #64748b;
      margin-bottom: 0.45rem;
      font-family: ui-monospace, monospace;
    }
    .quar-occurrence {
      font-size: 0.78rem;
      padding: 0.35rem 0;
      border-top: 1px dashed #fecaca;
      margin-top: 0.25rem;
    }
    .quar-loc { font-family: ui-monospace, monospace; color: #7f1d1d; }
    .quar-snippet {
      color: #475569;
      margin-top: 0.2rem;
      padding-left: 0.5rem;
      border-left: 2px solid #cbd5e1;
      font-style: italic;
      word-break: break-word;
    }
  </style>
</head>
<body>
  <header>
    <h1>
      Sanitizer Run Analytics
      <span style="font-size:0.75rem;font-weight:400;">
        <a href="run_summary.json">summary.json</a>
        <a href="file_manifest.jsonl">manifest</a>
        <a href="validation_report.json">validation</a>
        <a href="pii_transformations.csv">transformations</a>
        <a href="pii_quarantine.csv">quarantine</a>
      </span>
    </h1>
    <div class="meta" id="run-meta"></div>
  </header>

  <div class="stats" id="stats"></div>

  <main>
    <section class="panel">
      <h2>Entity ↔ File network</h2>
      <div id="network"></div>
      <div class="legend" id="legend"></div>
    </section>

    <aside class="panel">
      <h2>Quarantine (unmapped)</h2>
      <div class="quar-list" id="quarantine"></div>
    </aside>
  </main>

  <script type="application/json" id="dashboard-data">
__DASHBOARD_DATA__
  </script>

  <script>
    const data = JSON.parse(document.getElementById("dashboard-data").textContent);
    const s = data.summary;

    // --- Header meta --------------------------------------------------
    document.getElementById("run-meta").textContent =
      `Run ${s.run_id} · ${s.started_at} → ${s.completed_at} · status: ${s.run_status}`;

    // --- Stat tiles ---------------------------------------------------
    const totalReplacements = Object.values(s.replacements).reduce((a, b) => a + b, 0);
    const totalUnmapped = Object.values(s.unmapped).reduce((a, b) => a + b, 0);
    const stats = [
      { label: "Files Discovered", value: s.files_discovered },
      { label: "Processed", value: s.files_processed },
      { label: "Skipped", value: s.files_skipped_unsupported, klass: s.files_skipped_unsupported > 0 ? "warn" : "" },
      { label: "Failed", value: s.files_failed, klass: s.files_failed > 0 ? "fail" : "" },
      { label: "Empty", value: s.empty_files, klass: s.empty_files > 0 ? "warn" : "" },
      { label: "Mapped Replacements", value: totalReplacements },
      { label: "Unmapped Records", value: totalUnmapped, klass: totalUnmapped > 0 ? "warn" : "" },
      { label: "Validation",
        value: s.validation.passed ? "PASSED" : "FAILED",
        klass: s.validation.passed ? "success" : "fail" },
    ];
    document.getElementById("stats").innerHTML = stats.map(st =>
      `<div class="stat ${st.klass || ''}">
         <div class="label">${st.label}</div>
         <div class="value">${st.value}</div>
       </div>`).join("");

    // --- Network graph ------------------------------------------------
    const groupColors = {
      file:         { background: "#dbeafe", border: "#3b82f6" },
      person:       { background: "#dcfce7", border: "#22c55e" },
      organization: { background: "#fed7aa", border: "#f97316" },
      email:        { background: "#e9d5ff", border: "#a855f7" },
      phone:        { background: "#fce7f3", border: "#ec4899" },
    };

    const nodes = new vis.DataSet(data.graph.nodes.map(n => ({
      ...n,
      shape: n.group === "file" ? "box" : "ellipse",
      color: groupColors[n.group] || { background: "#e2e8f0", border: "#94a3b8" },
      font: { size: 12, face: "ui-monospace, monospace" },
    })));
    const edges = new vis.DataSet(data.graph.edges.map(e => ({
      ...e,
      arrows: "to",
      smooth: { type: "continuous" },
      color: { color: "#94a3b8", highlight: "#0f172a" },
    })));

    const network = new vis.Network(
      document.getElementById("network"),
      { nodes, edges },
      {
        layout: {
          randomSeed: 42,
          improvedLayout: true,
        },
        physics: {
          enabled: true,
          solver: "barnesHut",
          barnesHut: {
            gravitationalConstant: -8000,
            centralGravity: 0.3,
            springLength: 140,
            springConstant: 0.04,
            damping: 0.5,
            avoidOverlap: 0.6,
          },
          stabilization: {
            enabled: true,
            iterations: 1000,
            updateInterval: 50,
            fit: true,
          },
          timestep: 0.4,
          adaptiveTimestep: true,
          minVelocity: 0.75,
        },
        nodes: { borderWidth: 2 },
        edges: {
          width: 1,
          scaling: { min: 1, max: 6 },
          smooth: { type: "continuous", roundness: 0.2 },
        },
        interaction: {
          hover: true,
          tooltipDelay: 100,
          dragNodes: true,
          dragView: true,
          zoomView: true,
        },
      }
    );

    // Freeze the layout once it has settled. Without this the
    // force-directed simulation keeps running indefinitely, which
    // makes the graph feel jittery and never quite stable. After
    // stabilization the user can still drag nodes (they just stay
    // where dragged) and pan/zoom the view.
    network.once("stabilizationIterationsDone", () => {
      network.setOptions({ physics: { enabled: false } });
    });

    // --- Legend -------------------------------------------------------
    document.getElementById("legend").innerHTML = Object.entries(groupColors).map(
      ([k, v]) =>
        `<span class="legend-item">
           <span class="legend-dot ${k === 'file' ? 'box' : ''}" style="background:${v.background};border:2px solid ${v.border};"></span>
           ${k}
         </span>`
    ).join("");

    // --- Quarantine panel --------------------------------------------
    const quarEl = document.getElementById("quarantine");
    if (data.quarantine.length === 0) {
      quarEl.innerHTML =
        `<div class="quar-item empty">No unmapped values - clean run.</div>`;
    } else {
      quarEl.innerHTML = data.quarantine.map(q => `
        <div class="quar-item">
          <div class="quar-header">
            <span class="quar-kind">${q.kind}</span>${escapeHtml(q.value)}
          </div>
          <div class="quar-meta">
            hash ${q.value_hash} · ${q.count} occurrence${q.count === 1 ? "" : "s"}
          </div>
          ${q.occurrences.map(o => `
            <div class="quar-occurrence">
              <div class="quar-loc">${escapeHtml(o.file)} @ ${escapeHtml(o.location)}</div>
              <div class="quar-snippet">${escapeHtml(o.snippet)}</div>
            </div>
          `).join("")}
        </div>
      `).join("");
    }

    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;",
        '"': "&quot;", "'": "&#39;"
      }[c]));
    }
  </script>
</body>
</html>
"""

# Inline the CDN URL so it's a single source of truth at the top of
# the file rather than buried in the template string.
_HTML_TEMPLATE = _HTML_TEMPLATE.replace("__VIS_NETWORK_CDN__", VIS_NETWORK_CDN)

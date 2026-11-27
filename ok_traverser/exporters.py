"""
Multi-format graph and path exporters:
  - HTML (XSS-safe interactive cards + standalone SVG/Canvas subgraph visualizer)
  - JSON (structured paths, nodes, edges)
  - CSV (UTF-8 BOM tabular export for Excel/BI)
  - GEXF (Gephi / Maltego / NetworkX compatible XML)
"""

import csv
import html as html_lib
import json
import os
import sys
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Set, Tuple

from .config import Config


def export_paths_json(
    paths: List[List[Dict[str, str]]],
    edges: List[Tuple[str, str]],
    user_meta: Dict[str, Dict[str, str]],
    filepath: str,
) -> str:
    payload = {
        "paths_count": len(paths),
        "paths": paths,
        "nodes_count": len(user_meta),
        "edges_count": len(edges),
        "edges": [{"source": u, "target": v} for u, v in edges],
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return os.path.abspath(filepath)


def export_paths_csv(
    paths: List[List[Dict[str, str]]],
    filepath: str,
) -> str:
    with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["path_index", "step", "id", "name", "friend_count", "url"])
        for p_idx, path in enumerate(paths, 1):
            for step_idx, node in enumerate(path):
                writer.writerow([
                    p_idx,
                    step_idx,
                    node.get("id", ""),
                    node.get("name", ""),
                    node.get("friend_count", ""),
                    node.get("url", ""),
                ])
    return os.path.abspath(filepath)


def export_graph_gexf(
    paths: List[List[Dict[str, str]]],
    edges: List[Tuple[str, str]],
    user_meta: Dict[str, Dict[str, str]],
    filepath: str,
) -> str:
    """Exports discovered subgraph and highlighted shortest paths into GEXF 1.2 XML."""
    path_node_ids: Set[str] = {str(n["id"]) for p in paths for n in p}
    node_ids: Set[str] = set(path_node_ids)
    for u, v in edges[:5000]:  # cap massive exploratory graphs cleanly
        node_ids.add(str(u))
        node_ids.add(str(v))

    gexf = ET.Element("gexf", {
        "xmlns": "http://www.gexf.net/1.2draft",
        "version": "1.2",
    })
    graph = ET.SubElement(gexf, "graph", {"mode": "static", "defaultedgetype": "undirected"})

    attrs = ET.SubElement(graph, "attributes", {"class": "node"})
    ET.SubElement(attrs, "attribute", {"id": "0", "title": "url", "type": "string"})
    ET.SubElement(attrs, "attribute", {"id": "1", "title": "on_shortest_path", "type": "boolean"})

    nodes_el = ET.SubElement(graph, "nodes")
    for nid in sorted(node_ids):
        meta = user_meta.get(nid, {})
        label = str(meta.get("name", f"ID {nid}"))
        node_el = ET.SubElement(nodes_el, "node", {"id": nid, "label": label})
        attvalues = ET.SubElement(node_el, "attvalues")
        ET.SubElement(attvalues, "attvalue", {"for": "0", "value": str(meta.get("url", f"https://ok.ru/profile/{nid}"))})
        ET.SubElement(attvalues, "attvalue", {"for": "1", "value": "true" if nid in path_node_ids else "false"})

    edges_el = ET.SubElement(graph, "edges")
    seen_e: Set[Tuple[str, str]] = set()
    eid = 0
    for u, v in edges[:5000]:
        a, b = str(u), str(v)
        pair = (min(a, b), max(a, b))
        if pair in seen_e or a == b:
            continue
        seen_e.add(pair)
        ET.SubElement(edges_el, "edge", {"id": str(eid), "source": a, "target": b})
        eid += 1

    tree = ET.ElementTree(gexf)
    if hasattr(ET, "indent"):
        ET.indent(tree, space="  ")
    tree.write(filepath, encoding="utf-8", xml_declaration=True)
    return os.path.abspath(filepath)


def render_html_report(
    paths: List[List[Dict[str, str]]],
    excluded_names: Optional[List[str]] = None,
    filename: str = Config.report_file,
    auto_open: bool = True,
) -> str:
    """
    Renders an XSS-safe interactive HTML report displaying K shortest paths
    with step badges, friend counts, and profile links.
    """
    sections_html: List[str] = []

    for idx, path in enumerate(paths, 1):
        cards_html: List[str] = []
        total_steps = max(0, len(path) - 1)
        for i, p in enumerate(path):
            is_first = i == 0
            is_last = i == len(path) - 1
            if is_first:
                badge = '<span class="badge start">🏁 Начальный узел (A)</span>'
            elif is_last:
                badge = '<span class="badge end">🎯 Целевой узел (B)</span>'
            else:
                badge = f'<span class="badge link">🤝 Звено {i}</span>'

            safe_name = html_lib.escape(str(p.get("name", f"ID {p.get('id', '')}")))
            safe_id = html_lib.escape(str(p.get("id", "")))
            safe_url = html_lib.escape(str(p.get("url", f"https://ok.ru/profile/{safe_id}")), quote=True)
            raw_name = str(p.get("name", ""))
            avatar_letter = html_lib.escape(raw_name[0].upper()) if raw_name and raw_name[0].isalpha() else "👤"
            fc_val = p.get("friend_count")
            fc_str = f" • Контактов: {html_lib.escape(str(fc_val))}" if fc_val is not None else ""

            cards_html.append(
                f"""
                <div class="card">
                    {badge}
                    <div class="avatar">{avatar_letter}</div>
                    <div class="name">{safe_name}</div>
                    <div class="meta">ID: {safe_id}{fc_str}</div>
                    <a href="{safe_url}" target="_blank" rel="noopener noreferrer" class="link-btn">Открыть в OK.ru ↗</a>
                </div>
                """
            )
            if not is_last:
                cards_html.append('<div class="arrow">➜</div>')

        path_label = f"Цепочка #{idx} • Рукопожатий: {total_steps}"
        sections_html.append(
            f"""
            <div class="path-section">
                <div class="path-title">{html_lib.escape(path_label)}</div>
                <div class="flow">{"".join(cards_html)}</div>
            </div>
            """
        )

    excluded_text = ""
    if excluded_names:
        safe_excl = ", ".join(html_lib.escape(str(x)) for x in excluded_names)
        excluded_text = f"<div class='excluded-note'>🚫 <strong>Исключены из поиска:</strong> {safe_excl}</div>"

    primary_steps = max(0, len(paths[0]) - 1) if paths else 0
    html_content = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ok-ru-friends-pathfinder — Результаты поиска</title>
    <style>
        :root {{
            --ok-orange: #ee8208;
            --ok-hover: #d57303;
            --bg-page: #f2f4f7;
            --card-bg: #ffffff;
            --text-main: #1f2937;
            --text-muted: #6b7280;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-page);
            color: var(--text-main);
            margin: 0;
            padding: 40px 20px;
        }}
        .wrap {{
            max-width: 1250px;
            margin: 0 auto;
            background: var(--card-bg);
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.07);
        }}
        .header {{ text-align: center; margin-bottom: 30px; }}
        h1 {{ color: var(--ok-orange); font-size: 32px; margin: 0 0 10px 0; }}
        .subtitle {{ font-size: 16px; color: var(--text-muted); }}
        .summary-bar {{
            display: inline-block;
            margin-top: 15px;
            padding: 8px 18px;
            background: #eef9f1;
            border-radius: 30px;
            color: #2e7d32;
            font-weight: 600;
            font-size: 15px;
        }}
        .excluded-note {{
            margin-top: 15px;
            padding: 10px 16px;
            background: #fff8e6;
            border-left: 4px solid #f59e0b;
            border-radius: 6px;
            font-size: 14px;
            color: #78350f;
            display: inline-block;
        }}
        .path-section {{ margin-top: 25px; }}
        .path-title {{ font-weight: 700; font-size: 15px; color: var(--text-muted); margin-bottom: 10px; }}
        .flow {{
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            justify-content: center;
            gap: 15px;
            padding: 30px 20px;
            background: #fafafa;
            border-radius: 16px;
            border: 1px solid #e5e7eb;
        }}
        .card {{
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 16px;
            padding: 24px 20px;
            width: 230px;
            text-align: center;
            box-shadow: 0 4px 12px rgba(0,0,0,0.04);
            transition: transform 0.2s, box-shadow 0.2s;
        }}
        .card:hover {{ transform: translateY(-5px); box-shadow: 0 8px 24px rgba(0,0,0,0.1); }}
        .avatar {{
            width: 68px;
            height: 68px;
            background: linear-gradient(135deg, #f59e0b, #ee8208);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #ffffff;
            font-size: 28px;
            font-weight: 700;
            margin: 0 auto 14px;
            box-shadow: 0 4px 10px rgba(238, 130, 8, 0.3);
        }}
        .name {{ font-weight: 700; font-size: 16px; margin-bottom: 6px; word-break: break-word; }}
        .meta {{ color: var(--text-muted); font-size: 13px; margin-bottom: 16px; }}
        .badge {{
            display: inline-block;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            padding: 4px 10px;
            border-radius: 20px;
            margin-bottom: 14px;
            letter-spacing: 0.5px;
        }}
        .badge.start {{ background: #eff6ff; color: #2563eb; }}
        .badge.end {{ background: #fef2f2; color: #dc2626; }}
        .badge.link {{ background: #f3f4f6; color: #4b5563; }}
        .link-btn {{
            display: block;
            background: var(--ok-orange);
            color: #ffffff;
            text-decoration: none;
            padding: 9px 12px;
            border-radius: 10px;
            font-size: 13px;
            font-weight: 600;
            transition: background 0.15s;
        }}
        .link-btn:hover {{ background: var(--ok-hover); }}
        .arrow {{ font-size: 32px; color: var(--ok-orange); font-weight: 900; user-select: none; }}
    </style>
</head>
<body>
    <div class="wrap">
        <div class="header">
            <h1>🤝 Граф рукопожатий OK.ru</h1>
            <div class="subtitle">Кратчайшие пути связей между профилями в социальной сети</div>
            <div>
                <span class="summary-bar">Найдено цепочек: {len(paths)} • Мин. длина: {primary_steps} рукопожатий</span>
            </div>
            {excluded_text}
        </div>
        {"".join(sections_html)}
    </div>
</body>
</html>
"""
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html_content)
    abs_path = os.path.abspath(filename)
    if auto_open:
        try:
            if sys.platform.startswith("win"):
                os.startfile(abs_path)
        except Exception:
            pass
    return abs_path

#!/usr/bin/env python3
"""Crucible bundler: assemble 7-section static index.html + tarball.

Sections:
  Overview · KG · Replay · Agents (real vs synth badge) · Posts ·
  Interviews (R1/R2/R3 tabs) · Reports (Pass A/B/C side-by-side links)

KPI cards: real_agents=8 / synthetic=3 / posts=N / pairs=K /
           R3_with_pushback=X (agents that named a counter-fact)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
from html import escape
from pathlib import Path


def _load(p):
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None
    return d.get("body") if isinstance(d, dict) and "body" in d else d


def _unwrap(d):
    if isinstance(d, dict) and "data" in d:
        return d["data"]
    return d


def _md_to_html(md: str) -> str:
    if not md:
        return ""
    md = escape(md)
    md = re.sub(r"^###### (.+)$", r"<h6>\1</h6>", md, flags=re.M)
    md = re.sub(r"^##### (.+)$", r"<h5>\1</h5>", md, flags=re.M)
    md = re.sub(r"^#### (.+)$", r"<h4>\1</h4>", md, flags=re.M)
    md = re.sub(r"^### (.+)$", r"<h3>\1</h3>", md, flags=re.M)
    md = re.sub(r"^## (.+)$", r"<h2>\1</h2>", md, flags=re.M)
    md = re.sub(r"^# (.+)$", r"<h1>\1</h1>", md, flags=re.M)
    md = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", md)
    md = re.sub(r"`([^`]+)`", r"<code>\1</code>", md)
    # crude table: leave as preformatted
    md = re.sub(r"\n\n", r"</p><p>", md)
    return f"<p>{md}</p>"


def build(out_dir: Path):
    raw = out_dir / "raw"
    manifest = json.loads((out_dir / "manifest.json").read_text())
    sim_id = manifest.get("simulation_id", "")
    plat = manifest.get("platform", "twitter")
    title = manifest.get("requirement", "Crucible-sim")[:80]

    profiles = _unwrap(_load(raw / "art_profiles.json") or {})
    posts = _unwrap(_load(raw / "art_posts.json") or {})
    graph = _unwrap(_load(raw / "art_graph_data.json") or {})

    profile_list = profiles.get("profiles") or (profiles if isinstance(profiles, list) else [])
    posts_list = posts.get("posts") or (posts if isinstance(posts, list) else [])

    # synthetic agents → ids set
    synth_blob = _load(out_dir / "synthetic_agents.json") or {}
    synth_configs = synth_blob.get("written_configs", [])
    synth_ids = {c["agent_id"] for c in synth_configs}
    synth_meta = {c["agent_id"]: c for c in synth_configs}
    real_count = len(profile_list) - len(synth_ids)

    # Agent name map
    name_by_uid = {}
    for prof in profile_list:
        try:
            uid = int(prof.get("user_id"))
        except (TypeError, ValueError):
            continue
        nm = prof.get("name") or f"agent_{uid}"
        name_by_uid[uid] = nm

    # ---- KG nodes/edges
    nodes = graph.get("nodes") or graph.get("entities") or []
    edges = graph.get("edges") or graph.get("relationships") or []
    vis_nodes = []
    for n in nodes[:200]:
        nid = n.get("uuid") or n.get("id") or n.get("name")
        label = n.get("name") or n.get("label") or str(nid)[:8]
        group = n.get("entity_type") or n.get("type") or "Entity"
        vis_nodes.append({"id": nid, "label": label, "group": group,
                          "title": (n.get("summary") or "")[:300]})
    vis_edges = []
    for e in edges[:400]:
        f = e.get("source_node_uuid") or e.get("from") or e.get("source")
        t = e.get("target_node_uuid") or e.get("to") or e.get("target")
        ftype = e.get("fact_type") or e.get("name") or e.get("type") or ""
        fact = e.get("fact") or ""
        if f and t:
            vis_edges.append({"from": f, "to": t, "label": ftype,
                              "title": fact[:300]})

    # ---- agents html with badge
    profiles_html = []
    for p in profile_list:
        try:
            uid = int(p.get("user_id"))
        except (TypeError, ValueError):
            continue
        is_synth = uid in synth_ids
        badge = ('<span class="badge synth">SYNTHETIC</span>' if is_synth
                 else '<span class="badge real">REAL</span>')
        name = p.get("name") or f"agent_{uid}"
        bio = (p.get("description") or "")[:600]
        meta = synth_meta.get(uid, {})
        prior = meta.get("private_prior", "")
        conflicts = ", ".join(meta.get("conflicts_with", []))
        synth_tail = ""
        if is_synth:
            synth_tail = (f'<div class="synth-prior"><b>private_prior:</b> '
                          f'{escape(prior)}<br><b>conflicts_with:</b> '
                          f'{escape(conflicts)}</div>')
        profiles_html.append(
            f'<div class="profile">{badge} <b>{escape(name)}</b> '
            f'<span class="tag">id={uid}</span>'
            f'<p>{escape(bio)}</p>{synth_tail}</div>'
        )

    # ---- posts
    posts_html = []
    for p in posts_list[:300]:
        uid = p.get("user_id")
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            uid = -1
        author = name_by_uid.get(uid, p.get("author_name") or "?")
        is_synth = uid in synth_ids
        author_tag = (f'<b style="color:#f78166">@{escape(author)}</b>' if is_synth
                      else f'<b>@{escape(author)}</b>')
        round_n = p.get("created_at")
        likes = p.get("num_likes", p.get("like_count", 0))
        rt = p.get("num_shares", p.get("repost_count", 0))
        content = p.get("content") or p.get("text") or ""
        posts_html.append(
            f'<div class="post"><div class="meta">{author_tag} · round {escape(str(round_n))}'
            f' · ❤ {likes} · ↻ {rt}</div>'
            f'<div class="content">{escape(content)}</div></div>'
        )

    # ---- interviews tabs
    iv_path = out_dir / "interviews_r1_r2_r3.json"
    iv_html_r1 = iv_html_r2 = iv_html_r3 = ""
    iv_summary = {"r1": 0, "r2": 0, "r3": 0, "r3_pushback": 0, "pairs": 0}
    if iv_path.exists():
        iv = json.loads(iv_path.read_text())
        for it in iv.get("R1_self_statement", []):
            ans = it.get("answer", "")
            if ans:
                iv_summary["r1"] += 1
            iv_html_r1 += (
                f'<div class="post"><div class="meta"><b>@{escape(it["agent_name"])}</b></div>'
                f'<div class="content"><b style="color:#79c0ff">Q:</b> {escape(it["question"])}<br><br>'
                f'<b style="color:#3fb950">A:</b> {escape(ans)}</div></div>'
            )
        for it in iv.get("R2_cross_fire", []):
            ans = it.get("answer", "")
            if ans:
                iv_summary["r2"] += 1
            iv_html_r2 += (
                f'<div class="post"><div class="meta"><b>@{escape(it["agent_name"])}</b> '
                f'<span class="tag">topic={escape(it.get("topic_key",""))}</span> '
                f'<span class="tag">vs @{escape(it.get("facing","?"))}</span></div>'
                f'<div class="content"><b style="color:#79c0ff">Q:</b> {escape(it["question"])}<br><br>'
                f'<b style="color:#3fb950">A:</b> {escape(ans)}</div></div>'
            )
        for it in iv.get("R3_weakest_claim", []):
            ans = it.get("answer", "")
            if ans:
                iv_summary["r3"] += 1
                # crude pushback heuristic: contains 反方/反驳/不对/质疑/数字/缺陷/漏洞/manipulat/dispute
                if any(k in ans for k in ("反方", "反驳", "不对", "质疑", "缺陷", "漏洞",
                                          "low liquid", "noise", "manipulat", "dispute",
                                          "wrong", "misleading", "误导", "偏差")):
                    iv_summary["r3_pushback"] += 1
            iv_html_r3 += (
                f'<div class="post"><div class="meta"><b>@{escape(it["agent_name"])}</b></div>'
                f'<div class="content"><b style="color:#79c0ff">Q:</b> {escape(it["question"])}<br><br>'
                f'<b style="color:#3fb950">A:</b> {escape(ans)}</div></div>'
            )
        iv_summary["pairs"] = len(iv.get("pairs", []))

    # ---- reports A/B/C
    pass_a = (out_dir / "report_pass_A.md").read_text() if (out_dir / "report_pass_A.md").exists() else ""
    pass_b = (out_dir / "report_pass_B.md").read_text() if (out_dir / "report_pass_B.md").exists() else ""
    pass_c = (out_dir / "report_pass_C_gap.md").read_text() if (out_dir / "report_pass_C_gap.md").exists() else ""

    has_replay = (out_dir / "replay.gif").exists()
    replay_html = ""
    if has_replay:
        replay_html = (
            '<section id="replay"><h2>Replay · 11-agent activity</h2>'
            '<p style="color:#8b949e;font-size:13px">从 SQLite trace 渲染。'
            'orange = synthetic agent, blue = real.</p>'
            '<img src="replay.gif" style="max-width:100%;border:1px solid #30363d;border-radius:6px"></section>'
        )

    # KPIs
    posts_count = len(posts_list)

    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crucible-sim · {escape(title)}</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;
  margin:0;background:#0e1117;color:#e6edf3;line-height:1.55}}
header{{padding:24px 32px;border-bottom:1px solid #30363d;background:#161b22}}
h1{{margin:0 0 4px;font-size:20px}}
header .meta{{color:#8b949e;font-size:13px}}
nav{{display:flex;gap:14px;padding:0 32px;border-bottom:1px solid #30363d;background:#0d1117;flex-wrap:wrap}}
nav a{{padding:12px 0;color:#58a6ff;text-decoration:none;border-bottom:2px solid transparent;font-size:14px}}
nav a:hover{{border-color:#58a6ff}}
section{{padding:24px 32px;max-width:1100px;margin:0 auto}}
h2{{font-size:18px;margin:0 0 16px;border-bottom:1px solid #30363d;padding-bottom:8px}}
h3{{font-size:15px;margin:14px 0 6px;color:#c9d1d9}}
.post{{background:#161b22;border:1px solid #30363d;border-radius:6px;
  padding:12px 16px;margin-bottom:12px}}
.post .meta{{color:#8b949e;font-size:12px;margin-bottom:6px}}
.post .content{{white-space:pre-wrap;word-break:break-word;font-size:14px}}
.profile{{background:#161b22;border:1px solid #30363d;border-radius:6px;
  padding:12px 16px;margin-bottom:12px}}
.tag{{display:inline-block;background:#1f2937;color:#9ca3af;border-radius:4px;
  padding:2px 8px;font-size:11px;margin-left:6px}}
.badge{{display:inline-block;border-radius:4px;padding:2px 8px;font-size:10px;font-weight:bold;margin-right:6px}}
.badge.real{{background:#1f6feb;color:#fff}}
.badge.synth{{background:#f78166;color:#000}}
.synth-prior{{background:#21262d;border-left:3px solid #f78166;padding:8px 12px;margin-top:8px;
  font-size:12px;color:#9ca3af}}
#graph{{width:100%;height:520px;background:#161b22;border:1px solid #30363d;
  border-radius:6px}}
code{{background:#1f2937;padding:1px 4px;border-radius:3px;font-size:90%}}
table{{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px}}
th,td{{border:1px solid #30363d;padding:6px 10px;text-align:left}}
th{{background:#161b22}}
.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  gap:12px;margin:12px 0}}
.kpi .card{{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:12px}}
.kpi .card b{{display:block;font-size:22px}}
.kpi .card span{{color:#8b949e;font-size:12px}}
.tabs{{display:flex;gap:6px;margin:12px 0}}
.tabs button{{background:#21262d;color:#8b949e;border:1px solid #30363d;
  padding:6px 14px;border-radius:6px;font-size:13px;cursor:pointer}}
.tabs button.active{{background:#1f6feb;color:#fff;border-color:#1f6feb}}
.report-grid{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-top:8px}}
.report-grid .card{{background:#161b22;border:1px solid #30363d;border-radius:6px;
  padding:14px;max-height:660px;overflow:auto}}
.report-grid h3{{margin-top:0;color:#58a6ff}}
.report-grid p{{font-size:13px}}
@media (max-width:980px){{.report-grid{{grid-template-columns:1fr}}}}
</style></head><body>

<header>
  <h1>Crucible-sim · {escape(title)}</h1>
  <div class="meta">simulation_id <code>{escape(str(sim_id))}</code> · platform <code>{escape(plat)}</code> · {len(profile_list)} agents ({real_count} real + {len(synth_ids)} synthetic) · {posts_count} posts · {len(nodes)} kg-nodes / {len(edges)} kg-edges</div>
</header>

<nav>
  <a href="#overview">Overview</a>
  <a href="#kg">KG</a>
  {'<a href="#replay">Replay</a>' if has_replay else ''}
  <a href="#agents">Agents</a>
  <a href="#posts">Posts</a>
  <a href="#interviews">Interviews</a>
  <a href="#reports">Reports</a>
</nav>

<section id="overview">
  <h2>Overview</h2>
  <div class="kpi">
    <div class="card"><b>{real_count}</b><span>Real agents</span></div>
    <div class="card"><b>{len(synth_ids)}</b><span>Synthetic agents</span></div>
    <div class="card"><b>{posts_count}</b><span>Posts</span></div>
    <div class="card"><b>{iv_summary['pairs']}</b><span>Disagreement pairs</span></div>
    <div class="card"><b>{iv_summary['r1']}/{iv_summary['r2']}/{iv_summary['r3']}</b><span>R1/R2/R3 answered</span></div>
    <div class="card"><b>{iv_summary['r3_pushback']}/{iv_summary['r3'] or 1}</b><span>R3 with pushback</span></div>
    <div class="card"><b>{len(nodes)}</b><span>KG nodes</span></div>
    <div class="card"><b>{len(edges)}</b><span>KG edges</span></div>
  </div>
  <p><b>Briefing requirement:</b> {escape(manifest.get('requirement',''))}</p>
</section>

<section id="kg">
  <h2>Zep Knowledge Graph</h2>
  <div id="graph"></div>
</section>

{replay_html}

<section id="agents">
  <h2>Agents (orange badge = synthetic)</h2>
  {''.join(profiles_html) or '<i>no profiles</i>'}
</section>

<section id="posts">
  <h2>Posts (first 300)</h2>
  {''.join(posts_html) or '<i>no posts</i>'}
</section>

<section id="interviews">
  <h2>Interviews · 3 rounds</h2>
  <div class="tabs">
    <button onclick="showTab('r1')" id="tab-r1" class="active">R1 self-statement ({iv_summary['r1']})</button>
    <button onclick="showTab('r2')" id="tab-r2">R2 cross-fire ({iv_summary['r2']})</button>
    <button onclick="showTab('r3')" id="tab-r3">R3 weakest-claim ({iv_summary['r3']})</button>
  </div>
  <div id="iv-r1">{iv_html_r1 or '<i>no R1</i>'}</div>
  <div id="iv-r2" style="display:none">{iv_html_r2 or '<i>no R2</i>'}</div>
  <div id="iv-r3" style="display:none">{iv_html_r3 or '<i>no R3</i>'}</div>
</section>

<section id="reports">
  <h2>Reports · 三轮并列</h2>
  <div class="report-grid">
    <div class="card">
      <h3>Pass A · Neutral synthesis</h3>
      <p style="color:#8b949e;font-size:11px">MiroFish ReACT, glm-4.5-air</p>
      {_md_to_html(pass_a) or '<i>not generated</i>'}
    </div>
    <div class="card">
      <h3>Pass B · Sharp perspective</h3>
      <p style="color:#8b949e;font-size:11px">glm-4.6, opinionated</p>
      {_md_to_html(pass_b) or '<i>not generated</i>'}
    </div>
    <div class="card">
      <h3>Pass C · Gap audit</h3>
      <p style="color:#8b949e;font-size:11px">glm-4.6, missing-angle audit</p>
      {_md_to_html(pass_c) or '<i>not generated</i>'}
    </div>
  </div>
</section>

<script>
function showTab(t){{
  ['r1','r2','r3'].forEach(k=>{{
    document.getElementById('iv-'+k).style.display = (k===t)?'block':'none';
    document.getElementById('tab-'+k).classList.toggle('active', k===t);
  }});
}}
const nodes = new vis.DataSet({json.dumps(vis_nodes)});
const edges = new vis.DataSet({json.dumps(vis_edges)});
const network = new vis.Network(document.getElementById('graph'),
  {{nodes,edges}}, {{
    nodes:{{shape:'dot',size:12,font:{{color:'#e6edf3',size:11}}}},
    edges:{{arrows:'to',color:{{color:'#888',opacity:0.6}},
            font:{{color:'#9ca3af',size:9}},smooth:false}},
    physics:{{stabilization:{{iterations:120}},barnesHut:{{springConstant:0.02}}}},
    groups:{{Person:{{color:'#58a6ff'}},Organization:{{color:'#3fb950'}},
            CloudProvider:{{color:'#f85149'}},MediaOutlet:{{color:'#d29922'}}}}
  }});
</script>
</body></html>"""

    (out_dir / "index.html").write_text(html)
    print(f"  wrote {out_dir/'index.html'} ({len(html)} bytes)")

    # tarball — name from out_dir basename so non-AWS runs aren't misnamed
    tar_path = out_dir / f"{out_dir.name}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        for name in ["index.html", "manifest.json", "preflight.json",
                     "synthetic_agents.json", "disagreement_pairs.json",
                     "interviews_r1_r2_r3.json", "replay.gif",
                     "report_pass_A.md", "report_pass_B.md",
                     "report_pass_C_gap.md"]:
            p = out_dir / name
            if p.exists():
                tf.add(p, arcname=name)
        for p in sorted(raw.glob("*.json")):
            tf.add(p, arcname=f"raw/{p.name}")
    size = tar_path.stat().st_size
    print(f"  wrote {tar_path} ({size/1024:.1f} KB)")
    return tar_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out).resolve()
    if not (out / "manifest.json").exists():
        print(f"[FAIL] {out}/manifest.json not found")
        sys.exit(1)
    build(out)


if __name__ == "__main__":
    main()

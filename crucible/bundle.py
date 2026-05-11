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


DISCLAIMER_MD_FIRSTLINE = "> ⚠️ 真实 agent 在 OASIS posts 时间线中沉默"


def _build_disclaimer_text(m: dict) -> tuple[str, str]:
    """Return (markdown_blockquote, html_banner_inner) for a degraded run."""
    rp = m.get("real_posts", 0)
    tp = m.get("total_posts", 0)
    rpr = m.get("real_post_ratio", 0.0) or 0.0
    sa = m.get("silent_real_agents", 0)
    tra = m.get("total_real_agents", 0)
    srr = m.get("silent_real_ratio", 0.0) or 0.0
    md = (
        f"{DISCLAIMER_MD_FIRSTLINE}\n"
        f"> This OASIS posts timeline over-represents synthetic voices: "
        f"{rp}/{tp} posts ({rpr:.0%}) came from real agents; "
        f"{sa}/{tra} real agents ({srr:.0%}) stayed silent.\n"
        f"> The R1/R3 interview rounds (where all agents responded) are the "
        f"balanced view; the Posts section in `index.html` is not. "
        f"See `representation_metrics.json` for raw numbers.\n\n"
    )
    html_inner = (
        f"<b>⚠️ Real-agent silence</b> — {rp}/{tp} posts ({rpr:.0%}) from real agents; "
        f"{sa}/{tra} real agents ({srr:.0%}) stayed silent. "
        f"Posts timeline below over-represents synthetic voices; "
        f"prefer R1/R3 interview rounds for the balanced view."
    )
    return md, html_inner


def _load_metrics_and_disclaimer(out_dir: Path):
    """Read representation_metrics.json. Returns (metrics_dict_or_None,
    disclaimer_md_or_empty, disclaimer_html_or_empty). When the file is missing
    OR degraded_real_silent is not strictly True, both disclaimer strings are
    empty (so bundle.py walks the legacy path).
    """
    p = out_dir / "representation_metrics.json"
    if not p.exists():
        return None, "", ""
    try:
        m = json.loads(p.read_text())
    except Exception as e:
        print(f"[WARN] bundle: representation_metrics.json unreadable ({e}); "
              f"skipping disclaimer.")
        return None, "", ""
    if m.get("degraded_real_silent") is None:
        print(f"[WARN] bundle: representation_metrics.degraded_real_silent is null "
              f"(error={m.get('error')!r}, missing={m.get('missing')}); "
              f"skipping disclaimer.")
        return m, "", ""
    if m.get("degraded_real_silent") is not True:
        return m, "", ""
    md, html_inner = _build_disclaimer_text(m)
    return m, md, html_inner


def _prepend_disclaimer_idempotent(md_path: Path, disclaimer: str):
    """Prepend `disclaimer` to md_path's contents, unless its first line already
    starts with the disclaimer marker. No-op when the file does not exist."""
    if not md_path.exists():
        return False
    txt = md_path.read_text()
    if txt.lstrip().startswith(DISCLAIMER_MD_FIRSTLINE):
        return False
    md_path.write_text(disclaimer + txt)
    return True


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

    # Representation metrics + disclaimer (must run BEFORE the report .md files
    # are read on line ~205 — otherwise HTML report cards would render the old
    # un-disclaimed text while the disk md files get the disclaimer prepended.
    rep_metrics, disclaimer_md, disclaimer_html = _load_metrics_and_disclaimer(out_dir)
    if disclaimer_md:
        for name in ("report_pass_A.md", "report_pass_B.md", "report_pass_C_gap.md"):
            _prepend_disclaimer_idempotent(out_dir / name, disclaimer_md)

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
        # Zep KG returns entity type via `labels: [...]`, not `entity_type`.
        # Without this fallback every node lands in "Entity" and color-grouping
        # in the network viz collapses to one color.
        labels = n.get("labels") or []
        group = (n.get("entity_type") or n.get("type")
                 or (labels[0] if isinstance(labels, list) and labels else None)
                 or "Entity")
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

    # ---- posts (grouped by round = created_at, ascending)
    def _post_round_key(p):
        v = p.get("created_at")
        try:
            return int(v)
        except (TypeError, ValueError):
            return -1

    posts_by_round = {}
    for p in posts_list[:300]:
        posts_by_round.setdefault(_post_round_key(p), []).append(p)

    def _uid(p):
        try:
            return int(p.get("user_id"))
        except (TypeError, ValueError):
            return -1

    # OASIS quote_post field-semantics quirk: when a post has a non-empty
    # quote_content AND content == "the original post being quoted", the LLM's
    # actual new utterance lives in quote_content (NOT content). We resolve the
    # speaker's real text per-post via this helper; without it the timeline
    # looks like everyone is parroting the same line.
    orig_content_by_id = {p.get("post_id"): (p.get("content") or "").strip()
                          for p in posts_list if p.get("original_post_id") is None}

    def _speaker_text(p):
        """Return (utterance, quoted_excerpt_or_none).
        utterance = what this speaker actually wrote this turn.
        quoted_excerpt = the original post being referenced, if any.
        """
        content = (p.get("content") or p.get("text") or "").strip()
        quote = (p.get("quote_content") or "").strip()
        orig_id = p.get("original_post_id")
        if orig_id and quote:
            orig_text = orig_content_by_id.get(orig_id, "")
            # If content == the original being quoted, the LLM's true new
            # utterance is in quote_content (OASIS field-swap quirk).
            if orig_text and content and content[:60] == orig_text[:60]:
                return quote, content
            # Normal case: content is the new utterance, quote_content is what's
            # being quoted.
            return content, quote
        return content, None

    # Cross-round summary: real/synth count per round
    round_summary_rows = []
    for rn in sorted(posts_by_round.keys()):
        g = posts_by_round[rn]
        r_in = sum(1 for p in g if _uid(p) not in synth_ids)
        round_summary_rows.append(
            f'<tr><td>Round {rn}</td><td>{len(g)}</td><td>{r_in}</td>'
            f'<td>{len(g)-r_in}</td></tr>'
        )

    posts_summary_html = (
        '<div class="oasis-note">'
        '<b>本次 OASIS posts 时间线分布：</b>'
        '<table style="margin-top:6px"><thead><tr>'
        '<th>Round</th><th>Total</th><th>Real</th><th>Synth</th></tr></thead>'
        f'<tbody>{"".join(round_summary_rows)}</tbody></table>'
        '<div style="margin-top:8px;color:#8b949e;font-size:12px">'
        'Round 1+ 由合成 agent 主导是 default 模式预期行为（synth 注入是为了在真实 agent 沉默时持续施压）。'
        '想看真实 agent 的多视角，去 <a href="#real-voices">Real-Agent Voices</a> section（step 6 R1/R2/R3 采访）。</div>'
        '</div>'
    )

    posts_html = [posts_summary_html]
    for round_n in sorted(posts_by_round.keys()):
        group = posts_by_round[round_n]
        group.sort(key=lambda x: (x.get("post_id") or 0))
        real_in_round = sum(1 for p in group if _uid(p) not in synth_ids)
        synth_in_round = len(group) - real_in_round
        posts_html.append(
            f'<h3 class="round-header">Round {escape(str(round_n))}'
            f' <span class="round-count">({len(group)} posts · '
            f'{real_in_round} real / {synth_in_round} synth)</span></h3>'
        )

        for p in group:
            uid = _uid(p)
            author = name_by_uid.get(uid, p.get("author_name") or "?")
            is_synth = uid in synth_ids
            author_tag = (f'<b style="color:#f78166">@{escape(author)}</b>' if is_synth
                          else f'<b>@{escape(author)}</b>')
            likes = p.get("num_likes", p.get("like_count", 0))
            rt = p.get("num_shares", p.get("repost_count", 0))
            utter, quoted = _speaker_text(p)
            quoted_html = ""
            if quoted:
                qhead = quoted[:200] + ("…" if len(quoted) > 200 else "")
                quoted_html = (
                    '<div class="quoted-orig">↳ 引用 '
                    f'<span style="color:#8b949e">post #{p.get("original_post_id")}</span>: '
                    f'<span class="quoted-text">«{escape(qhead)}»</span></div>'
                )
            posts_html.append(
                f'<div class="post"><div class="meta">{author_tag}'
                f' · ❤ {likes} · ↻ {rt}</div>'
                f'<div class="content">{escape(utter)}</div>'
                f'{quoted_html}</div>'
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

    # ---- Real-Agent Voices (R1/R2/R3 aggregated by real agent)
    # OASIS posts timeline often falls silent for real agents in round 1+
    # (LLM picks do_nothing or quote_post). The interviews capture each real
    # person's full voice; surface them grouped by speaker so the user sees
    # the differentiated views the briefing was supposed to test.
    real_voices_html = ""
    real_voices_count = 0
    if iv_path.exists():
        by_agent = {}
        for it in iv.get("R1_self_statement", []):
            aid = it.get("agent_id")
            if aid is None or aid in synth_ids:
                continue
            by_agent.setdefault(aid, {
                "name": it.get("agent_name", "?"), "r1": [], "r2": [], "r3": []
            })["r1"].append(it)
        for it in iv.get("R2_cross_fire", []):
            aid = it.get("agent_id")
            if aid is None or aid in synth_ids:
                continue
            by_agent.setdefault(aid, {
                "name": it.get("agent_name", "?"), "r1": [], "r2": [], "r3": []
            })["r2"].append(it)
        for it in iv.get("R3_weakest_claim", []):
            aid = it.get("agent_id")
            if aid is None or aid in synth_ids:
                continue
            by_agent.setdefault(aid, {
                "name": it.get("agent_name", "?"), "r1": [], "r2": [], "r3": []
            })["r3"].append(it)

        real_voices_count = len(by_agent)
        for aid in sorted(by_agent.keys()):
            v = by_agent[aid]
            blocks = []
            for r in v["r1"]:
                blocks.append(
                    '<div class="rv-block"><div class="rv-tag">R1 self-statement</div>'
                    f'<div class="rv-q">Q: {escape(r.get("question",""))}</div>'
                    f'<div class="rv-a">{escape(r.get("answer",""))}</div></div>'
                )
            for r in v["r2"]:
                facing = r.get("facing", "?")
                topic = r.get("topic_key", "")
                blocks.append(
                    f'<div class="rv-block"><div class="rv-tag">R2 cross-fire vs @{escape(facing)}'
                    f' <span class="tag">topic={escape(topic)}</span></div>'
                    f'<div class="rv-q">Q: {escape(r.get("question",""))}</div>'
                    f'<div class="rv-a">{escape(r.get("answer",""))}</div></div>'
                )
            for r in v["r3"]:
                blocks.append(
                    '<div class="rv-block"><div class="rv-tag">R3 weakest-claim challenge</div>'
                    f'<div class="rv-q">Q: {escape(r.get("question",""))}</div>'
                    f'<div class="rv-a">{escape(r.get("answer",""))}</div></div>'
                )
            if blocks:
                real_voices_html += (
                    f'<div class="rv-agent"><h3>@{escape(v["name"])} '
                    f'<span class="tag">id={aid}</span></h3>'
                    + "".join(blocks) + "</div>"
                )

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
.round-header{{margin:24px 0 12px;padding-bottom:6px;border-bottom:1px solid #30363d;
  color:#58a6ff;font-size:16px;font-weight:600}}
.round-header:first-child{{margin-top:0}}
.round-count{{color:#8b949e;font-weight:400;font-size:13px;margin-left:8px}}
.oasis-note{{background:#1c2530;border-left:3px solid #58a6ff;padding:10px 14px;
  margin:0 0 14px;color:#c9d1d9;font-size:13px;line-height:1.5;border-radius:0 4px 4px 0}}
.oasis-note a{{color:#58a6ff}}
.echo-note{{margin-top:8px;padding-top:8px;border-top:1px dashed #30363d;
  color:#8b949e;font-size:12px;font-style:italic}}
.echo-note b{{color:#d29922}}
.quoted-orig{{margin-top:8px;padding:8px 12px;background:#0d1117;border-left:3px solid #30363d;
  color:#8b949e;font-size:12px;border-radius:0 4px 4px 0}}
.quoted-text{{color:#c9d1d9;font-style:italic}}
.rv-agent{{background:#161b22;border:1px solid #30363d;border-radius:6px;
  padding:14px 18px;margin-bottom:18px}}
.rv-agent h3{{margin:0 0 10px;color:#58a6ff;font-size:15px;font-weight:600}}
.rv-agent h3 .tag{{font-weight:400;font-size:11px}}
.rv-block{{background:#0d1117;border-left:3px solid #3fb950;padding:10px 14px;
  margin-top:10px;border-radius:0 4px 4px 0}}
.rv-tag{{color:#79c0ff;font-size:12px;margin-bottom:6px;font-weight:600}}
.rv-q{{color:#8b949e;font-size:13px;margin-bottom:8px;font-style:italic}}
.rv-a{{color:#c9d1d9;font-size:14px;line-height:1.55;white-space:pre-wrap}}
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
.warn-banner{{background:#3d1414;border:1px solid #f85149;color:#ffdcd7;
  padding:14px 24px;font-size:13px;line-height:1.6;margin:0}}
.warn-banner b{{color:#ff7b72}}
.warn-banner-inline{{background:#3d1414;border:1px solid #f85149;color:#ffdcd7;
  padding:10px 14px;font-size:12px;line-height:1.5;margin:0 0 14px;border-radius:6px}}
</style></head><body>

<header>
  <h1>Crucible-sim · {escape(title)}</h1>
  <div class="meta">simulation_id <code>{escape(str(sim_id))}</code> · platform <code>{escape(plat)}</code> · {len(profile_list)} agents ({real_count} real + {len(synth_ids)} synthetic) · {posts_count} posts · {len(nodes)} kg-nodes / {len(edges)} kg-edges</div>
</header>
{(f'<div class="warn-banner">{disclaimer_html}</div>') if disclaimer_html else ''}

<nav>
  <a href="#overview">Overview</a>
  <a href="#kg">KG</a>
  {'<a href="#replay">Replay</a>' if has_replay else ''}
  <a href="#agents">Agents</a>
  <a href="#posts">Posts</a>
  <a href="#real-voices">Real Voices</a>
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
  <h2>Posts (first 300, grouped by round)</h2>
  {(f'<div class="warn-banner-inline">{disclaimer_html}</div>') if disclaimer_html else ''}
  {''.join(posts_html) or '<i>no posts</i>'}
</section>

<section id="real-voices">
  <h2>Real-Agent Voices ({real_voices_count} agents) <span class="round-count">— 来自 step 6 R1/R2/R3 私下采访</span></h2>
  <div class="oasis-note">
    OASIS posts 时间线在 round 1+ 通常被合成 agent 主导（真实 agent 的 LLM 倾向选 do_nothing 或 quote_post 兜底，造成内容复读）。
    本 section 把 step 6 R1/R2/R3 三轮采访按真实 agent 聚合，让每个真实人物的差异化立场完整呈现。
    同一 agent 的 R1（自陈）/ R2（针对对手）/ R3（挑战 briefing 弱点）可以横向对比，看 ta 在不同压力下是否一致。
  </div>
  {real_voices_html or '<i>no real-agent voices captured</i>'}
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
      <p style="color:#8b949e;font-size:11px">MiroFish ReACT; direct LLM fallback if needed</p>
      {_md_to_html(pass_a) or '<i>not generated</i>'}
    </div>
    <div class="card">
      <h3>Pass B · Sharp perspective</h3>
      <p style="color:#8b949e;font-size:11px">configured Pass B model, opinionated</p>
      {_md_to_html(pass_b) or '<i>not generated</i>'}
    </div>
    <div class="card">
      <h3>Pass C · Gap audit</h3>
      <p style="color:#8b949e;font-size:11px">configured Pass C model, missing-angle audit</p>
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
                     "report_pass_C_gap.md",
                     "representation_metrics.json"]:
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

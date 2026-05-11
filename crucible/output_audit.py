#!/usr/bin/env python3
"""Pre-bundle output audit — catch rendering hazards before they ship.

Lessons from past incidents:
- OASIS quote_post swaps content / quote_content semantically; rendering the
  raw `content` makes everyone look like they're parroting one line.
- Zep KG nodes use `labels: [...]`, not `entity_type` — bundle rendering with
  the wrong field collapses every node into one color group.
- Real agents may go silent in round 1+ when synthetic agents are present;
  worth flagging before publication so the renderer can surface it.

This audit is read-only: it inspects raw artifacts, prints warnings, and
writes `output_audit.json`. bundle.py reads the audit's flags so renderers
can degrade gracefully (e.g. swap fields, surface notices).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_body(p: Path):
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    body = d.get("body", d) if isinstance(d, dict) else d
    if isinstance(body, dict):
        return body.get("data", body)
    return body


def audit(out_dir: Path) -> dict:
    raw = out_dir / "raw"
    findings = []

    # ---- post field-swap detection (OASIS quote_post quirk)
    posts_data = _load_body(raw / "art_posts.json") or {}
    posts = posts_data.get("posts") if isinstance(posts_data, dict) else posts_data
    posts = posts or []
    orig_by_id = {p.get("post_id"): (p.get("content") or "").strip()
                  for p in posts if p.get("original_post_id") is None}
    swap_count = 0
    swap_samples = []
    for p in posts:
        oid = p.get("original_post_id")
        if not oid:
            continue
        c = (p.get("content") or "").strip()
        q = (p.get("quote_content") or "").strip()
        if not c or not q:
            continue
        orig = orig_by_id.get(oid, "")
        if orig and c[:60] == orig[:60]:
            swap_count += 1
            if len(swap_samples) < 3:
                swap_samples.append({
                    "post_id": p.get("post_id"),
                    "user_id": p.get("user_id"),
                    "content_head": c[:60],
                    "quote_content_head": q[:60],
                    "original_post_id": oid,
                })
    if swap_count > 0:
        findings.append({
            "id": "post_field_swap",
            "severity": "high",
            "count": swap_count,
            "message": (f"OASIS quote_post 字段错位：{swap_count} 条 post 的 "
                        "content == 被 quote 的原文，agent 真实发言在 quote_content "
                        "字段。bundle 必须用 _speaker_text 修正。"),
            "samples": swap_samples,
        })

    # ---- round 1+ real-agent silence
    sim_dir = None
    manifest_p = out_dir / "manifest.json"
    if manifest_p.exists():
        try:
            sim_dir = Path(json.loads(manifest_p.read_text())["sim_dir"])
        except Exception:
            pass
    synth_ids = set()
    synth_blob_p = out_dir / "synthetic_agents.json"
    if synth_blob_p.exists():
        try:
            sb = json.loads(synth_blob_p.read_text())
            for c in sb.get("written_configs", []) or []:
                aid = c.get("agent_id")
                if aid is not None:
                    synth_ids.add(aid)
        except Exception:
            pass
    rounds = {}
    for p in posts:
        try:
            rd = int(p.get("created_at"))
            uid = int(p.get("user_id"))
        except (TypeError, ValueError):
            continue
        rounds.setdefault(rd, {"real": 0, "synth": 0})
        if uid in synth_ids:
            rounds[rd]["synth"] += 1
        else:
            rounds[rd]["real"] += 1
    silent_rounds = [rd for rd, c in rounds.items()
                     if rd >= 1 and c["real"] == 0 and c["synth"] > 0]
    if silent_rounds:
        findings.append({
            "id": "real_silent_post_round0",
            "severity": "medium",
            "rounds": silent_rounds,
            "message": (f"真实 agent 在 round {silent_rounds} 完全沉默；"
                        "OASIS LLM 决策让真实 agent 选 do_nothing。"
                        "考虑加强 lift-real 的 profile 注入。"),
        })

    # ---- KG node group field
    graph = _load_body(raw / "art_graph_data.json") or {}
    nodes = graph.get("nodes") or graph.get("entities") or []
    nodes_no_type = sum(
        1 for n in nodes
        if not (n.get("entity_type") or n.get("type"))
    )
    if nodes_no_type and nodes:
        labels_present = sum(1 for n in nodes if n.get("labels"))
        if labels_present:
            findings.append({
                "id": "kg_node_type_via_labels",
                "severity": "low",
                "message": (f"{nodes_no_type}/{len(nodes)} KG 节点没有 entity_type 字段，"
                            f"但 {labels_present} 个有 labels[]。bundle 必须 fallback 到 labels[0] "
                            "才能正确分组着色。"),
            })

    # ---- profile completeness
    profiles_data = _load_body(raw / "art_profiles.json") or {}
    profiles = (profiles_data.get("profiles")
                if isinstance(profiles_data, dict) else profiles_data) or []
    empty_profiles = [p.get("user_id") for p in profiles
                      if not (p.get("user_char") or p.get("description"))]
    if empty_profiles:
        findings.append({
            "id": "empty_profiles",
            "severity": "high",
            "count": len(empty_profiles),
            "user_ids": empty_profiles[:10],
            "message": f"{len(empty_profiles)} 个 agent 没有 user_char/description profile。",
        })

    # ---- duplicate content across rounds (legitimate concern, not always bug)
    content_authors = {}
    for p in posts:
        c = (p.get("content") or "").strip()
        if not c:
            continue
        try:
            uid = int(p.get("user_id"))
            rd = int(p.get("created_at"))
        except (TypeError, ValueError):
            continue
        content_authors.setdefault(c, []).append((rd, uid))
    multi_author_dup = [
        (c, lst) for c, lst in content_authors.items()
        if len({uid for _, uid in lst}) > 1
    ]
    if multi_author_dup:
        findings.append({
            "id": "cross_author_duplicate_content",
            "severity": "info",
            "count": len(multi_author_dup),
            "message": (f"{len(multi_author_dup)} 条 content 被 2+ 个不同 user "
                        "发出。如配合 post_field_swap 出现，多半是字段错位假象。"),
        })

    # ---- summary
    severity_order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    findings.sort(key=lambda x: severity_order.get(x.get("severity"), 9))
    counts = {"high": 0, "medium": 0, "low": 0, "info": 0}
    for f in findings:
        counts[f.get("severity", "info")] = counts.get(f.get("severity", "info"), 0) + 1
    verdict = ("fail" if counts["high"] > 0 else
               "warn" if counts["medium"] > 0 else
               "pass")
    return {
        "status": "ok",
        "verdict": verdict,
        "counts": counts,
        "findings": findings,
        "round_breakdown": rounds,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero on any high-severity finding")
    args = ap.parse_args()

    res = audit(args.out)
    out_p = args.out / "output_audit.json"
    out_p.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    counts = res["counts"]
    print(f"[output_audit] verdict={res['verdict']} "
          f"high={counts['high']} medium={counts['medium']} "
          f"low={counts['low']} info={counts['info']}")
    for f in res["findings"]:
        print(f"  [{f['severity']}] {f['id']}: {f['message']}")
    if args.strict and counts["high"] > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()

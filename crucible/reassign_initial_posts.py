#!/usr/bin/env python3
"""Reassign poster_agent_id of round-0 initial_posts by content semantics.

MiroFish backend's `_assign_initial_post_agents` matches posts to agents by
poster_type rotation only (not content). When briefing has many Person-type
quotes, this systematically misattributes (e.g. CEO 张一鸣 ends up "saying"
72-year-old retiree 老顾's words).

This step runs AFTER synth-agent injection but BEFORE /api/simulation/start.
It reads simulation_config.json + twitter_profiles.csv, asks the LLM to map
each post's content to the most-likely speaker, and rewrites poster_agent_id
in-place. Idempotent: re-running on already-correct config is a no-op.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import requests


def _load_profiles(sim_dir: Path) -> dict:
    """user_id -> {name, brief} from twitter_profiles.csv."""
    profiles = {}
    csv_path = sim_dir / "twitter_profiles.csv"
    if not csv_path.exists():
        return profiles
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            uid = int(row["user_id"])
            # description is short, user_char is long. prefer description if
            # present, fall back to first ~200 chars of user_char.
            brief = (row.get("description") or row.get("user_char") or "").strip()
            if len(brief) > 220:
                brief = brief[:220].rstrip() + "…"
            profiles[uid] = {"name": row["name"], "brief": brief}
    return profiles


def _build_prompt(initial_posts: list, agents: list) -> str:
    agent_lines = []
    for a in agents:
        agent_lines.append(
            f"- id={a['id']} | {a['name']} | {a['role']} | {a['brief']}"
        )
    post_lines = []
    for i, p in enumerate(initial_posts):
        c = p.get("content", "").strip().replace("\n", " ")
        if len(c) > 200:
            c = c[:200] + "…"
        post_lines.append(f"- post_idx={i} | type={p.get('poster_type','?')} | \"{c}\"")
    return f"""你的任务：把每条 round-0 初始帖子（quote/statement）分配给**最合适的发布者 agent**。

判断依据：post 的内容语气、立场、第一人称视角、所述事实，是否与 agent 的身份/角色/profile 一致。

例如：
- "我厂去年 GPU 利用率 38%" 显然来自 SRE/工程师，不是 CEO 或退休老人
- "我对豆包的信任程度不值这个价" 是普通用户视角，不是公司官方
- "豆包推出付费订阅计划..." 是中性新闻叙述，可分给公司官号或媒体

候选 agents：
{chr(10).join(agent_lines)}

待分配 posts：
{chr(10).join(post_lines)}

约束：
1. 每条 post 必须分给一个 agent（用 agent.id）
2. 同一个 agent 不要被分配超过 1 条 post（除非 agent 数 < post 数）
3. 优先匹配 entity 名字直接出现在 content 里的（例如 content 包含"老顾"则优先分给 name=老顾 的 agent）
4. content 是公司官方叙述（含产品发布、价格表）→ 分给 Organization 类型 agent；不要分给 Person

只返回 JSON，不加任何 markdown 包裹：
{{"assignments": [{{"post_idx": 0, "agent_id": <int>}}, ...]}}
"""


def _call_llm(prompt: str, base_url: str, api_key: str, model: str, timeout: int = 90) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是文本归属判断助手。只输出严格 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    r = requests.post(url, json=payload, headers=headers, timeout=timeout)
    r.raise_for_status()
    body = r.json()
    content = body["choices"][0]["message"]["content"]
    return json.loads(content)


def reassign(sim_dir: Path, llm_base_url: str, llm_api_key: str, llm_model: str,
             dry_run: bool = False) -> dict:
    cfg_path = sim_dir / "simulation_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    initial_posts = cfg.get("event_config", {}).get("initial_posts", [])
    agent_configs = cfg.get("agent_configs", [])

    if not initial_posts:
        return {"status": "skipped", "reason": "no initial_posts"}
    if not agent_configs:
        return {"status": "skipped", "reason": "no agent_configs"}

    profiles = _load_profiles(sim_dir)

    # Build agent summary list
    agents = []
    for a in agent_configs:
        aid = a["agent_id"]
        prof = profiles.get(aid, {})
        agents.append({
            "id": aid,
            "name": a.get("entity_name") or prof.get("name") or f"agent_{aid}",
            "role": a.get("entity_type") or "Unknown",
            "brief": prof.get("brief") or "",
        })

    prompt = _build_prompt(initial_posts, agents)
    result = _call_llm(prompt, llm_base_url, llm_api_key, llm_model)
    assignments = result.get("assignments", [])

    if not assignments:
        return {"status": "failed", "reason": "llm returned empty assignments"}

    # Build mapping post_idx -> new agent_id
    new_map = {a["post_idx"]: a["agent_id"] for a in assignments
               if isinstance(a.get("post_idx"), int) and isinstance(a.get("agent_id"), int)}
    valid_ids = {a["id"] for a in agents}
    diff = []
    for i, p in enumerate(initial_posts):
        old_id = p.get("poster_agent_id")
        new_id = new_map.get(i)
        if new_id is None or new_id not in valid_ids:
            continue
        if new_id != old_id:
            diff.append({"post_idx": i, "old": old_id, "new": new_id,
                         "content_head": p.get("content", "")[:60]})
            if not dry_run:
                p["poster_agent_id"] = new_id

    if not dry_run:
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                            encoding="utf-8")

    return {
        "status": "ok",
        "total_posts": len(initial_posts),
        "reassigned": len(diff),
        "diff": diff,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sim-dir", required=True, type=Path)
    ap.add_argument("--llm-base-url", required=True)
    ap.add_argument("--llm-api-key", required=True)
    ap.add_argument("--llm-model", required=True)
    ap.add_argument("--out", type=Path, help="optional path to dump report json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    res = reassign(args.sim_dir, args.llm_base_url, args.llm_api_key,
                   args.llm_model, dry_run=args.dry_run)
    if args.out:
        args.out.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(f"[reassign] status={res.get('status')} reassigned={res.get('reassigned',0)}/{res.get('total_posts',0)}")
    for d in res.get("diff", [])[:10]:
        print(f"  post[{d['post_idx']}] {d['old']} → {d['new']}  «{d['content_head']}»")
    if res.get("status") not in ("ok", "skipped"):
        sys.exit(2)


if __name__ == "__main__":
    main()

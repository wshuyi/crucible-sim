#!/usr/bin/env python3
"""Layer 3a: scan posts to find stance disagreement pairs.

Reads art_posts.json + art_profiles.json, asks LLM to assign each agent a
stance label per topic, then identifies pairs with directly opposing stances
on the same topic. Outputs disagreement_pairs.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

from openai import OpenAI


PROMPT = """你是社交模拟的中立观察员。下面是某场 swarm-simulation 里每位 agent 的简介与代表性发帖。
请给每个 agent 在以下 4 个 topic 上打 stance 标签。

== Topics ==
{topics}

== Agents（含真实 + 合成）==
{agents_block}

== 输出（严格 JSON，不要 markdown fence、不要前言）==
{{
  "stances": [
    {{
      "agent_id": <int>,
      "agent_name": "...",
      "topic_stances": {{
        "topic_key_1": "supportive | opposed | neutral | mixed",
        "topic_key_2": "...",
        ...
      }},
      "key_quote": "ta 在帖子里最能体现核心立场的一句（≤100 字）"
    }},
    ...
  ]
}}

要求：
- topic_key 用提供的 topic 列表里的 key（不要中文）
- supportive/opposed 必须有帖子证据；模棱两可一律打 mixed；从未谈及打 neutral
- 每个 agent 必须出现，每个 topic 都必须给标签
"""


TOPIC_DEFS = [
    {"key": "concentration_risk_systemic",
     "label": "AWS 单点故障是系统性风险问题",
     "supportive_means": "认为这是 AWS/平台/政策层面的系统性问题，需监管/拆分",
     "opposed_means": "认为这是用户/企业自己 multi-region 没做好，怪 AWS 没道理"},
    {"key": "multicloud_vs_multiregion",
     "label": "应当 true multi-cloud 还是 active multi-region",
     "supportive_means": "明确支持 multi-cloud (AWS+GCP+Azure)",
     "opposed_means": "认为 multi-region within AWS 已足够"},
    {"key": "polymarket_38_credible",
     "label": "Polymarket 38% YES 是有意义的市场共识",
     "supportive_means": "把 38% 当作可信信号去引用",
     "opposed_means": "质疑 38% 背后的流动性/操纵/信号价值"},
    {"key": "amzn_drop_significant",
     "label": "AMZN 当日跌 1.6% 是有意义的市场反应",
     "supportive_means": "用 1.6% 论证投资者对 AWS 的担忧",
     "opposed_means": "认为 1.6% 在日内噪音范围内，且当日已部分修复，无信号"},
]


def build_prompt(profiles, posts):
    topics_block = "\n".join(
        f'- {t["key"]}: "{t["label"]}"  '
        f'(supportive = {t["supportive_means"]}; opposed = {t["opposed_means"]})'
        for t in TOPIC_DEFS
    )

    by_agent = defaultdict(list)
    for p in posts:
        uid = p.get("user_id")
        if isinstance(uid, str):
            try:
                uid = int(uid)
            except ValueError:
                continue
        by_agent[uid].append(p.get("content") or "")

    agent_blocks = []
    for prof in profiles:
        uid = prof.get("user_id")
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            continue
        name = prof.get("name") or f"agent_{uid}"
        bio = (prof.get("description") or prof.get("bio") or "")[:280]
        ps = by_agent.get(uid, [])
        sample = "\n    ".join(f"- {x[:280]}" for x in ps[:4]) or "(无发帖)"
        agent_blocks.append(
            f"### agent_id={uid}  name={name}\n"
            f"  bio: {bio}\n"
            f"  posts:\n    {sample}"
        )
    return PROMPT.format(topics=topics_block,
                         agents_block="\n\n".join(agent_blocks))


def llm_stance(client, model, prompt, *, max_tokens=8000):
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=max_tokens,
    )
    raw = (r.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


def find_pairs(stance_data, *, topic_meta):
    """Return list of {topic, pair: [a, b], a_stance, b_stance, a_quote, b_quote}."""
    by_topic = defaultdict(lambda: {"supportive": [], "opposed": []})
    quotes = {}
    for entry in stance_data.get("stances", []):
        aid = entry.get("agent_id")
        name = entry.get("agent_name")
        quotes[aid] = (name, entry.get("key_quote", ""))
        for tkey, lbl in (entry.get("topic_stances") or {}).items():
            if lbl in ("supportive", "opposed"):
                by_topic[tkey][lbl].append((aid, name))
    pairs = []
    for tkey, sides in by_topic.items():
        sup = sides["supportive"]
        opp = sides["opposed"]
        # Cartesian, but cap to top 2 per side per topic
        for a in sup[:2]:
            for b in opp[:2]:
                tmeta = next((t for t in topic_meta if t["key"] == tkey), None)
                pairs.append({
                    "topic_key": tkey,
                    "topic_label": tmeta["label"] if tmeta else tkey,
                    "supportive_agent": {"id": a[0], "name": a[1],
                                         "quote": quotes.get(a[0], ("", ""))[1]},
                    "opposed_agent": {"id": b[0], "name": b[1],
                                      "quote": quotes.get(b[0], ("", ""))[1]},
                })
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--llm-base-url",
                    default=os.environ.get("LLM_BASE_URL",
                                           "http://127.0.0.1:8011/v1"))
    ap.add_argument("--llm-api-key",
                    default=os.environ.get("LLM_API_KEY",
                                           os.environ.get("OPENROUTER_API_KEY",
                                           os.environ.get("ZAI_API_KEY", ""))))
    ap.add_argument("--llm-model",
                    default=os.environ.get("LLM_MODEL_NAME", "glm-4.7"))
    ap.add_argument("--max-pairs", type=int, default=8)
    args = ap.parse_args()

    raw = Path(args.results_dir) / "raw"
    profiles_blob = json.loads((raw / "art_profiles.json").read_text())
    posts_blob = json.loads((raw / "art_posts.json").read_text())
    profiles = (profiles_blob.get("body") or {}).get("data", {}).get("profiles", [])
    if not profiles:
        # /profiles/realtime returns {"profiles": [...]} directly
        profiles = (profiles_blob.get("body") or {}).get("profiles", [])
    posts = (posts_blob.get("body") or {}).get("data", {}).get("posts", [])
    if not profiles or not posts:
        print(f"[FAIL] missing profiles ({len(profiles)}) or posts ({len(posts)})")
        sys.exit(2)
    print(f"[OK] {len(profiles)} profiles, {len(posts)} posts")

    prompt = build_prompt(profiles, posts)
    client = OpenAI(api_key=args.llm_api_key, base_url=args.llm_base_url)
    t0 = time.time()
    stance_data = llm_stance(client, args.llm_model, prompt)
    print(f"[OK] LLM stance assigned in {time.time()-t0:.1f}s "
          f"({len(stance_data.get('stances', []))} agents)")

    pairs = find_pairs(stance_data, topic_meta=TOPIC_DEFS)
    pairs = pairs[:args.max_pairs]
    print(f"[OK] derived {len(pairs)} disagreement pairs")
    for p in pairs:
        print(f"  - [{p['topic_key']}] "
              f"{p['supportive_agent']['name'][:30]} vs "
              f"{p['opposed_agent']['name'][:30]}")

    Path(args.out).write_text(json.dumps(
        {"stances": stance_data,
         "topic_defs": TOPIC_DEFS,
         "pairs": pairs},
        ensure_ascii=False, indent=2))
    print(f"[OK] wrote {args.out}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Layer 4c: gap audit (Pass C).

Reads preflight.missing_angles + posts + R3 challenge results, asks the configured
LLM (default: glm-4.7) to write a structured "missing angle" audit:
1. 未探索角度（preflight 列了 N 个，sim 实际探索了几个？哪些零覆盖？）
2. 未出现的反方观点（哪些预期反方在 posts 里没出现？）
3. 被反复 parrot 的数字 + R3 是否戳穿（38%, 1.6%, 14天 等）
4. 下次 briefing 改进建议
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from openai import OpenAI


def _load_body(p):
    blob = json.loads(Path(p).read_text())
    body = blob.get("body") if isinstance(blob, dict) else blob
    return body or {}


PROMPT = """你是 swarm-simulation 的方法论审计员。下面给你 4 类输入：
A) 事前审计（preflight.json）
B) sim 中所有发帖（posts，按 agent 聚合）
C) R3 weakest-claim 信仰挑战的回答
D) R2 cross-fire 摘要（按话题）

写一份「盲区审计报告」，**严格 4 节**，每节小标题用二级标题。要求：

## 1. 未探索的视角
- 把 preflight.missing_angles 逐条列出来
- 对每条判断：本次 sim 是否真的探索了？依据是什么 post？还是零覆盖？
- 至少列 1 条**没人说过**的角度并解释为什么这是缺陷

## 2. 没出现的反方观点
- 列出 preflight.contested_points 里没在 posts 中真正对线过的立场
- 注意区分"嘴上说一句"和"形成实质反驳"
- 至少 2 条具体例子

## 3. 被 parrot 的数字 + R3 是否戳穿
- 把 preflight.numerical_claims 里的每个数字（如 38%、1.6%、14天）逐条审：
  - 它在 posts 里被引用了多少次？
  - R3 weakest-claim 答里有几位 agent 真的提出了反驳？反驳的力度如何？
- 最后给一个表格（markdown 表格）：claim | sim 引用数 | R3 反驳数 | 是否被有效挑战

## 4. 下次 briefing 应当如何改进
- 给出 4-6 条**可操作**的改进建议（如"加入一段 SRE 视角的 incident timeline"）
- 每条建议必须能解决前面 1-3 节里发现的具体盲区

== A) Preflight ==
{preflight}

== B) Posts (按 agent 聚合) ==
{posts_block}

== C) R3 weakest-claim 回答（weakest_claim: 「{weak}」）==
{r3_block}

== D) R2 cross-fire 摘要 ==
{r2_block}

只输出 Markdown 正文（不带 fence、不带前言、不要"以下是审计报告"开头）。
"""


def aggregate_posts(profiles, posts):
    by_agent = {}
    for p in profiles:
        try:
            uid = int(p.get("user_id"))
        except (TypeError, ValueError):
            continue
        by_agent[uid] = {"name": p.get("name") or f"agent_{uid}", "posts": []}
    for post in posts:
        try:
            uid = int(post.get("user_id"))
        except (TypeError, ValueError):
            continue
        if uid in by_agent:
            txt = (post.get("content") or "").strip()
            if txt:
                by_agent[uid]["posts"].append(txt)
    out = []
    for uid in sorted(by_agent.keys()):
        a = by_agent[uid]
        if not a["posts"]:
            continue
        ps = "\n".join(f"  - {p[:240]}" for p in a["posts"][:6])
        out.append(f"@{a['name']} (id={uid})\n{ps}")
    return "\n\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", required=True)
    ap.add_argument("--preflight", required=True)
    ap.add_argument("--interviews", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--llm-base-url",
                    default=os.environ.get("LLM_BASE_URL",
                                           "http://127.0.0.1:8011/v1"))
    ap.add_argument("--llm-api-key",
                    default=os.environ.get("LLM_API_KEY",
                                           os.environ.get("OPENROUTER_API_KEY",
                                           os.environ.get("ZAI_API_KEY", ""))))
    ap.add_argument("--llm-model",
                    default=os.environ.get("LLM_MODEL_C", "glm-4.7"))
    ap.add_argument("--max-tokens", type=int, default=8000)
    args = ap.parse_args()

    raw = Path(args.results_dir) / "raw"
    pre = json.loads(Path(args.preflight).read_text())
    interviews = json.loads(Path(args.interviews).read_text())

    pf_body = _load_body(raw / "art_profiles.json")
    profiles = pf_body.get("data", {}).get("profiles") or pf_body.get("profiles") or []
    pst_body = _load_body(raw / "art_posts.json")
    posts = pst_body.get("data", {}).get("posts") or pst_body.get("posts") or []
    posts_block = aggregate_posts(profiles, posts)

    r2 = interviews.get("R2_cross_fire", [])
    r2_lines = []
    for it in r2:
        ans = (it.get("answer") or "").replace("\n", " ").strip()
        if ans:
            r2_lines.append(f"- [{it.get('topic_key')}] @{it['agent_name']} (vs @{it.get('facing','?')}): {ans[:400]}")
    r2_block = "\n".join(r2_lines) or "(空)"

    r3 = interviews.get("R3_weakest_claim", [])
    r3_lines = []
    for it in r3:
        ans = (it.get("answer") or "").replace("\n", " ").strip()
        if ans:
            r3_lines.append(f"- @{it['agent_name']}: {ans[:500]}")
    r3_block = "\n".join(r3_lines) or "(空)"

    prompt = PROMPT.format(
        preflight=json.dumps(pre, ensure_ascii=False, indent=2)[:3500],
        posts_block=posts_block[:9000],
        weak=interviews.get("weakest_claim", ""),
        r3_block=r3_block[:6000],
        r2_block=r2_block[:5000],
    )
    client = OpenAI(api_key=args.llm_api_key, base_url=args.llm_base_url)
    t0 = time.time()
    r = client.chat.completions.create(
        model=args.llm_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.5, max_tokens=args.max_tokens,
    )
    md = (r.choices[0].message.content or "").strip()
    if md.startswith("```"):
        md = md.strip("`")
        if md.startswith("markdown"):
            md = md[8:]
        md = md.strip()
    Path(args.out).write_text(md)
    print(f"[OK] Pass C wrote {args.out} ({len(md)} chars in {time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()

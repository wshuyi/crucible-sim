#!/usr/bin/env python3
"""Post-sim attribution audit.

Cross-checks each generated post's content against the assigned agent's
profile/identity using LLM. Flags posts where the speaker is implausible
(e.g. a CEO "saying" a 72-year-old retiree's words).

Outputs:
  attribution_audit.json with:
    - total_audited
    - mismatches: [{post_id, user_id, name, content_head, expected_kind, reason}]
    - mismatch_ratio
    - verdict: "pass" | "warn" | "fail"

Threshold (default): mismatch_ratio > 0.20 → "warn"; > 0.40 → "fail".
Override via CRUCIBLE_ATTR_WARN / CRUCIBLE_ATTR_FAIL.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

import requests


def _load_profiles(sim_dir: Path) -> dict:
    profiles = {}
    csv_path = sim_dir / "twitter_profiles.csv"
    if not csv_path.exists():
        return profiles
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            uid = int(row["user_id"])
            brief = (row.get("description") or row.get("user_char") or "").strip()
            if len(brief) > 240:
                brief = brief[:240].rstrip() + "…"
            profiles[uid] = {"name": row["name"], "brief": brief}
    return profiles


def _load_posts(out_dir: Path) -> list:
    raw = out_dir / "raw" / "art_posts.json"
    if not raw.exists():
        return []
    body = json.loads(raw.read_text(encoding="utf-8"))
    data = body.get("body", body)
    if isinstance(data, dict):
        data = data.get("data", data)
    if isinstance(data, dict):
        return data.get("posts", [])
    return data if isinstance(data, list) else []


def _build_prompt(items: list) -> str:
    lines = []
    for it in items:
        c = it["content"][:180].replace("\n", " ")
        lines.append(
            f"- post_id={it['post_id']} | speaker_id={it['user_id']} ({it['name']}, {it['role']}) "
            f"| profile_brief: {it['brief']} | content: \"{c}\""
        )
    return f"""你的任务：判断每条 post 的 content 是否与其 speaker 的身份/profile 一致。

对每条 post，输出 verdict:
- "ok": content 与 speaker 身份一致（语气、视角、立场、所述事实合理）
- "mismatch": 明显不一致（如 CEO 说 72 岁退休老人的话，工程师说 CEO 战略，记者发出消费者投诉）

待审 posts：
{chr(10).join(lines)}

只返回 JSON，不加 markdown：
{{"results": [{{"post_id": <int>, "verdict": "ok"|"mismatch", "reason": "<不超过 30 字>"}}, ...]}}
"""


def _call_llm(prompt: str, base_url: str, api_key: str, model: str, timeout: int = 120) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是文本一致性审查助手。只输出严格 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    r = requests.post(url, json=payload, headers=headers, timeout=timeout)
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def audit(out_dir: Path, sim_dir: Path, llm_base_url: str, llm_api_key: str,
          llm_model: str, only_round0: bool = True, batch_size: int = 30) -> dict:
    posts = _load_posts(out_dir)
    profiles = _load_profiles(sim_dir)

    if only_round0:
        posts = [p for p in posts if p.get("created_at") in (0, "0")]

    items = []
    for p in posts:
        try:
            uid = int(p.get("user_id"))
        except (TypeError, ValueError):
            continue
        prof = profiles.get(uid, {})
        items.append({
            "post_id": p.get("post_id"),
            "user_id": uid,
            "name": prof.get("name") or "?",
            "role": "?",
            "brief": prof.get("brief") or "",
            "content": p.get("content") or "",
        })

    if not items:
        return {"status": "skipped", "reason": "no posts to audit"}

    all_results = []
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        prompt = _build_prompt(batch)
        try:
            res = _call_llm(prompt, llm_base_url, llm_api_key, llm_model)
            all_results.extend(res.get("results", []))
        except Exception as e:
            print(f"[attribution_audit] batch {i}-{i+batch_size} failed: {e}",
                  file=sys.stderr)

    # Build mismatch list
    by_id = {it["post_id"]: it for it in items}
    mismatches = []
    ok_count = 0
    for r in all_results:
        pid = r.get("post_id")
        verdict = (r.get("verdict") or "").lower()
        it = by_id.get(pid)
        if not it:
            continue
        if verdict == "mismatch":
            mismatches.append({
                "post_id": pid,
                "user_id": it["user_id"],
                "name": it["name"],
                "content_head": it["content"][:80],
                "reason": r.get("reason", ""),
            })
        elif verdict == "ok":
            ok_count += 1

    total = ok_count + len(mismatches)
    ratio = len(mismatches) / total if total > 0 else 0.0

    warn_th = float(os.environ.get("CRUCIBLE_ATTR_WARN", "0.20"))
    fail_th = float(os.environ.get("CRUCIBLE_ATTR_FAIL", "0.40"))
    if ratio >= fail_th:
        verdict = "fail"
    elif ratio >= warn_th:
        verdict = "warn"
    else:
        verdict = "pass"

    return {
        "status": "ok",
        "scope": "round_0" if only_round0 else "all_rounds",
        "total_audited": total,
        "ok_count": ok_count,
        "mismatch_count": len(mismatches),
        "mismatch_ratio": round(ratio, 3),
        "warn_threshold": warn_th,
        "fail_threshold": fail_th,
        "verdict": verdict,
        "mismatches": mismatches,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, type=Path, help="crucible run out_dir")
    ap.add_argument("--sim-dir", required=True, type=Path)
    ap.add_argument("--llm-base-url", required=True)
    ap.add_argument("--llm-api-key", required=True)
    ap.add_argument("--llm-model", required=True)
    ap.add_argument("--all-rounds", action="store_true",
                    help="audit all rounds (default: only round 0)")
    args = ap.parse_args()

    res = audit(args.out, args.sim_dir, args.llm_base_url,
                args.llm_api_key, args.llm_model,
                only_round0=not args.all_rounds)
    out_path = args.out / "attribution_audit.json"
    out_path.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    v = res.get("verdict", "?")
    n = res.get("total_audited", 0)
    m = res.get("mismatch_count", 0)
    r = res.get("mismatch_ratio", 0.0)
    print(f"[attribution_audit] verdict={v} mismatch={m}/{n} ratio={r:.2%}")
    for x in res.get("mismatches", [])[:5]:
        print(f"  post={x['post_id']} @{x['name']}: {x['reason']}")
        print(f"    «{x['content_head']}»")
    # exit non-zero on FAIL so CI / run.sh can decide
    if v == "fail":
        sys.exit(3)


if __name__ == "__main__":
    main()

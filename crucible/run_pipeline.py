#!/usr/bin/env python3
"""Crucible orchestrator: ontology -> build -> create -> prepare ->
inject 3 synthetic agents -> patch 24h -> start -> monitor -> pull artifacts.

Does NOT close env (interview comes later).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests


# ----- helpers (copied from mirofish-simulation/run_simulation.py) -----------


def _save(raw_dir, label, body, status):
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{label}.json").write_text(
        json.dumps({"_status": status, "body": body}, ensure_ascii=False, indent=2)
    )


def _unwrap(body):
    if isinstance(body, dict) and "success" in body and "data" in body:
        return body["data"]
    return body


def _err(body):
    if isinstance(body, dict) and body.get("success") is False:
        return body.get("error") or "unknown"
    return None


def _try_json(r):
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:4000]}


def post_json(s, url, payload, label, raw, *, timeout=300):
    r = s.post(url, json=payload, timeout=timeout)
    body = _try_json(r)
    _save(raw, label, body, r.status_code)
    if r.status_code >= 400 or _err(body):
        print(f"[FAIL] POST {label} HTTP {r.status_code}: {_err(body) or r.text[:300]}")
        sys.exit(2)
    print(f"[OK]   POST {label} -> {r.status_code}", flush=True)
    return _unwrap(body)


def post_multipart(s, url, files, form, label, raw, *, timeout=600):
    r = s.post(url, files=files, data=form, timeout=timeout)
    body = _try_json(r)
    _save(raw, label, body, r.status_code)
    if r.status_code >= 400 or _err(body):
        print(f"[FAIL] POST {label} HTTP {r.status_code}: {_err(body) or r.text[:300]}")
        sys.exit(2)
    print(f"[OK]   POST {label} -> {r.status_code}", flush=True)
    return _unwrap(body)


def get_json(s, url, label, raw, *, accept_404=True, timeout=120):
    r = s.get(url, timeout=timeout)
    body = _try_json(r)
    _save(raw, label, body, r.status_code)
    if r.status_code == 404 and accept_404:
        return None
    if r.status_code >= 400 or _err(body):
        print(f"[FAIL] GET {label} HTTP {r.status_code}: {_err(body) or r.text[:300]}")
        return None
    return _unwrap(body)


def poll(label, fn, *, interval, timeout, success, fail=None):
    start = time.time()
    last = None
    while True:
        body = fn()
        if body is None:
            time.sleep(interval)
            continue
        if fail:
            why = fail(body)
            if why:
                print(f"[FAIL] {label}: {why}")
                sys.exit(3)
        if success(body):
            return body
        keys = ("status", "state", "phase", "progress", "message",
                "current_round", "total_rounds", "runner_status",
                "twitter_current_round", "twitter_actions_count",
                "twitter_completed", "twitter_running",
                "config_generated", "profiles_count")
        snap = {k: body.get(k) for k in keys if k in body}
        if snap != last:
            print(f"  [{label}] {json.dumps(snap, ensure_ascii=False)}", flush=True)
            last = snap
        if time.time() - start > timeout:
            print(f"[TIMEOUT] {label} after {timeout}s")
            sys.exit(4)
        time.sleep(interval)


def _posts_count(s, base, sim_id, platform, raw):
    """Probe DB row count for posts. Returns int or None when count is not
    reliably known. We REFUSE to fall back to len(posts) when the response
    only carries the posts list, because the limit=1 query would always
    return 0 or 1 — that fake count would trip the plateau check on every
    real run and short-circuit healthy simulations."""
    body = get_json(s, f"{base}/api/simulation/{sim_id}/posts"
                          f"?platform={platform}&limit=1",
                    "06_posts_count_latest", raw,
                    accept_404=True, timeout=30)
    if not isinstance(body, dict):
        return None
    for k in ("count", "total"):
        v = body.get(k)
        if isinstance(v, int):
            return v
    return None  # plateau detection disabled when backend does not expose count/total


def monitor_run(s, base, sim_id, *, platform, run_timeout,
                plateau_threshold, wait_full_completion, raw_dir,
                interval=20):
    """Poll run-status AND posts-count. Exit on:
       (a) runner_status completed/finished/done
       (b) {platform}_completed flag flips true (or both flip in parallel)
       (c) posts count plateaus for >= plateau_threshold seconds with count > 0
           (soft-completion fallback for OASIS round-counter freezes).
    Prints state changes only."""
    start = time.time()
    last_snap = None
    last_count = None
    last_count_change = time.time()
    # Plateau soft-completion is only safe when one platform is being
    # monitored. In parallel mode the caller wants BOTH twitter & reddit to
    # finish; a twitter plateau alone must not end the run while reddit is
    # still progressing.
    plateau_enabled = not wait_full_completion
    plateau_disabled_warned = False
    while True:
        elapsed = time.time() - start
        body = get_json(s, f"{base}/api/simulation/{sim_id}/run-status",
                        "06_run_status_latest", raw_dir,
                        accept_404=True)
        if body is None:
            time.sleep(interval)
            continue

        if body.get("runner_status") in ("failed", "error"):
            why = body.get("error") or "runner_status=failed"
            print(f"[FAIL] run: {why}")
            sys.exit(3)

        ok = (body.get("runner_status") in ("completed", "finished", "done"))
        if not ok:
            tw_done = bool(body.get("twitter_completed"))
            rd_done = bool(body.get("reddit_completed"))
            if wait_full_completion:
                ok = tw_done and rd_done
            elif platform == "twitter":
                ok = tw_done
            elif platform == "reddit":
                ok = rd_done
            else:
                ok = tw_done or rd_done
        if ok:
            print(f"  [run] runner_status={body.get('runner_status')} "
                  f"twitter_completed={body.get('twitter_completed')} "
                  f"reddit_completed={body.get('reddit_completed')} "
                  f"(elapsed={elapsed:.0f}s)")
            return {"plateau_triggered": False,
                    "final_runner_status": body.get("runner_status"),
                    "final_posts_count": last_count,
                    "elapsed_s": int(elapsed)}

        # Plateau check
        count = _posts_count(s, base, sim_id, platform, raw_dir)
        if count is None and plateau_enabled and not plateau_disabled_warned:
            print("[INFO] backend /posts response does not expose count/total; "
                  "plateau soft-completion disabled, will rely on run-status "
                  "or --run-timeout.")
            plateau_disabled_warned = True
        if count != last_count:
            last_count = count
            last_count_change = time.time()
        plateau_s = time.time() - last_count_change
        if (plateau_enabled
                and count is not None and count > 0
                and plateau_s >= plateau_threshold):
            print(f"[WARN] OASIS round-counter has not advanced; posts count "
                  f"({count}) has been flat for {plateau_s:.0f}s "
                  f"(>= {plateau_threshold}s threshold). Treating as "
                  f"soft-completed and proceeding to artifact pull. "
                  f"runner_status={body.get('runner_status')} "
                  f"current_round={body.get('current_round')}")
            return {"plateau_triggered": True,
                    "final_runner_status": body.get("runner_status"),
                    "final_posts_count": count,
                    "elapsed_s": int(elapsed)}

        keys = ("status", "state", "phase", "progress", "message",
                "current_round", "total_rounds", "runner_status",
                "twitter_current_round", "twitter_actions_count",
                "twitter_completed", "twitter_running",
                "config_generated", "profiles_count")
        snap = {k: body.get(k) for k in keys if k in body}
        snap["posts_count"] = count
        # NOTE: plateau_s deliberately excluded from the snapshot — it ticks
        # every poll and would defeat the "log only on state change" gate.
        if snap != last_snap:
            print(f"  [run] {json.dumps(snap, ensure_ascii=False)}", flush=True)
            last_snap = snap

        if elapsed > run_timeout:
            print(f"[TIMEOUT] run after {run_timeout}s "
                  f"(posts_count={count}, plateau_s={plateau_s:.0f})")
            sys.exit(4)
        time.sleep(interval)
    # unreachable
    return {"plateau_triggered": False, "final_runner_status": None,
            "final_posts_count": last_count, "elapsed_s": int(time.time() - start)}


def _detect_sim_dir(backend, sim_id, *, hint=None):
    """Locate MiroFish's per-sim working directory on the local FS.

    Search order:
      1. Explicit --sim-dir-hint (if it's a directory; sim_id is appended)
      2. Env var MIROFISH_SIM_ROOT (its sim_id subdir)
      3. Common patterns under $HOME/Downloads/*/MiroFish/backend/uploads/simulations
    """
    if "127.0.0.1" not in backend and "localhost" not in backend:
        return None
    candidates = []
    if hint:
        candidates.append(Path(hint) / sim_id)
        candidates.append(Path(hint))
    env_root = os.environ.get("MIROFISH_SIM_ROOT")
    if env_root:
        candidates.append(Path(env_root) / sim_id)
    candidates.append(Path.home() / "Downloads")
    for c in candidates:
        if not c.exists():
            continue
        # Direct match (already <sim_dir>)
        if (c / "simulation_config.json").exists():
            return c
        # Search pattern under c
        for p in c.glob("*/MiroFish/backend/uploads/simulations"):
            d = p / sim_id
            if d.exists():
                return d
        d = c / sim_id
        if d.exists() and (d / "simulation_config.json").exists():
            return d
    return None


def _load_artifact_body(out_dir: Path, raw_name: str):
    """Load raw/<name>.json and unwrap _status/body shape; return None on miss."""
    p = out_dir / "raw" / raw_name
    if not p.exists():
        return None
    try:
        blob = json.loads(p.read_text())
    except Exception:
        return None
    body = blob.get("body") if isinstance(blob, dict) and "body" in blob else blob
    if isinstance(body, dict) and "data" in body and isinstance(body["data"], dict):
        return body["data"]
    return body


def _compute_representation_metrics(out_dir: Path, monitor_result: dict) -> dict:
    """Read raw/art_config.json + raw/art_posts.json + synthetic_agents.json
    and produce the representation_metrics shape consumed by bundle.py.

    Degraded judgment uses two OR-conditions, both env-overridable:
      silent_real_ratio >= CRUCIBLE_DEGRADED_SILENT_MIN  (default 0.75)
      real_post_ratio   <  CRUCIBLE_DEGRADED_REAL_RATIO_MAX (default 0.35)
    Either condition (with total_posts > 0) flips degraded_real_silent=True.
    """
    silent_min = float(os.environ.get("CRUCIBLE_DEGRADED_SILENT_MIN", "0.75"))
    real_ratio_max = float(os.environ.get("CRUCIBLE_DEGRADED_REAL_RATIO_MAX", "0.35"))
    base_meta = {
        "schema_version": 1,
        "thresholds": {
            "silent_real_ratio_min": silent_min,
            "real_post_ratio_max": real_ratio_max,
        },
        "plateau_triggered": bool(monitor_result.get("plateau_triggered")),
        "final_runner_status": monitor_result.get("final_runner_status"),
        "final_posts_count": monitor_result.get("final_posts_count"),
    }
    cfg = _load_artifact_body(out_dir, "art_config.json")
    posts_data = _load_artifact_body(out_dir, "art_posts.json")
    synth_path = out_dir / "synthetic_agents.json"
    synth_blob = None
    if synth_path.exists():
        try:
            synth_blob = json.loads(synth_path.read_text())
        except Exception:
            synth_blob = None
    missing = []
    if not isinstance(cfg, dict) or "agent_configs" not in cfg:
        missing.append("art_config.json")
    if not isinstance(posts_data, dict) or "posts" not in posts_data:
        missing.append("art_posts.json")
    if not isinstance(synth_blob, dict):
        missing.append("synthetic_agents.json")
    if missing:
        return {**base_meta, "error": "artifact_missing_or_corrupt",
                "missing": missing, "degraded_real_silent": None}

    written = synth_blob.get("written_configs") or []
    synth_ids = set()
    for w in written:
        try:
            synth_ids.add(int(w.get("agent_id")))
        except (TypeError, ValueError):
            continue
    all_agents = cfg.get("agent_configs") or []
    total_real = max(0, len(all_agents) - len(synth_ids))
    real_ids = set()
    for entry in all_agents:
        try:
            aid = int(entry.get("agent_id"))
        except (TypeError, ValueError):
            continue
        if aid not in synth_ids:
            real_ids.add(aid)

    posts = posts_data.get("posts") or []
    total_posts = len(posts)
    synth_posts = 0
    real_posts = 0
    real_voiced = set()
    for p in posts:
        try:
            uid = int(p.get("user_id"))
        except (TypeError, ValueError):
            continue
        if uid in synth_ids:
            synth_posts += 1
        elif uid in real_ids:
            real_posts += 1
            real_voiced.add(uid)

    real_post_ratio = (real_posts / total_posts) if total_posts else 0.0
    silent_real = max(0, total_real - len(real_voiced))
    silent_real_ratio = (silent_real / total_real) if total_real else 0.0

    reasons = []
    if total_posts > 0:
        if silent_real_ratio >= silent_min:
            reasons.append(f"silent_real_ratio>={silent_min}")
        if real_post_ratio < real_ratio_max:
            reasons.append(f"real_post_ratio<{real_ratio_max}")
    degraded = bool(reasons)

    return {
        **base_meta,
        "total_posts": total_posts,
        "synth_posts": synth_posts,
        "real_posts": real_posts,
        "real_post_ratio": round(real_post_ratio, 3),
        "total_real_agents": total_real,
        "silent_real_agents": silent_real,
        "silent_real_ratio": round(silent_real_ratio, 3),
        "degraded_real_silent": degraded,
        "degradation_reasons": reasons,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="http://127.0.0.1:5002")
    ap.add_argument("--doc", required=True)
    ap.add_argument("--requirement", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--preflight", required=True)
    ap.add_argument("--max-rounds", type=int, default=5)
    ap.add_argument("--platform", default="twitter",
                    choices=["twitter", "reddit", "parallel"])
    ap.add_argument("--prepare-timeout", type=int, default=1800)
    ap.add_argument("--build-timeout", type=int, default=1200)
    ap.add_argument("--run-timeout", type=int, default=5400)
    ap.add_argument("--mode",
                    choices=["default", "mirofish", "miroshark"],
                    default=os.environ.get("CRUCIBLE_MODE", "default"),
                    help="default=3 synth (skeptic+expert+stakeholder); "
                         "mirofish=0 synth, real entities only; "
                         "miroshark=5 synth (adds provocateur+futurist, wilder)")
    ap.add_argument("--sim-dir-hint",
                    default=os.environ.get("MIROFISH_SIM_ROOT"),
                    help="Optional path containing MiroFish/backend/uploads/simulations "
                         "(or pre-resolved <sim_dir>); else autodetect under $HOME/Downloads.")
    ap.add_argument("--llm-base-url",
                    default=os.environ.get("LLM_BASE_URL",
                                           "http://127.0.0.1:8011/v1"),
                    help="OpenAI-compatible endpoint. Examples: "
                         "http://127.0.0.1:8011/v1 (local glm-proxy), "
                         "https://openrouter.ai/api/v1 (OpenRouter), "
                         "https://api.openai.com/v1 (OpenAI).")
    ap.add_argument("--llm-api-key",
                    default=os.environ.get("LLM_API_KEY",
                                           os.environ.get("OPENROUTER_API_KEY",
                                           os.environ.get("ZAI_API_KEY", ""))))
    ap.add_argument("--llm-model",
                    default=os.environ.get("LLM_MODEL_NAME", "glm-4.7"),
                    help="Model slug for the configured base_url. Examples: "
                         "glm-4.7 (z-ai), anthropic/claude-haiku-4.5 (OpenRouter), "
                         "openai/gpt-5.4-mini (OpenRouter).")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    base = args.backend.rstrip("/")

    # 1. Ontology
    doc = Path(args.doc).resolve()
    files = {"files": (doc.name, doc.open("rb"),
                       "text/markdown" if doc.suffix in (".md", ".markdown") else "text/plain")}
    form = {"simulation_requirement": args.requirement,
            "project_name": f"crucible-{time.strftime('%Y%m%d-%H%M%S')}"}
    body = post_multipart(s, f"{base}/api/graph/ontology/generate",
                          files, form, "01_ontology", raw)
    project_id = body["project_id"]
    print(f"  project_id = {project_id}")

    # 2. Build graph
    body = post_json(s, f"{base}/api/graph/build",
                     {"project_id": project_id}, "02_build_kick", raw)
    task_id = body.get("task_id")
    poll("build",
         lambda: get_json(s, f"{base}/api/graph/task/{task_id}",
                          "02_build_status_latest", raw),
         interval=10, timeout=args.build_timeout,
         success=lambda b: b.get("status") in ("completed", "success", "done"),
         fail=lambda b: b.get("error") if b.get("status") in ("failed", "error") else None)
    print("  graph built")

    # 3. Create simulation
    body = post_json(s, f"{base}/api/simulation/create",
                     {"project_id": project_id,
                      "enable_twitter": True,
                      "enable_reddit": args.platform in ("reddit", "parallel")},
                     "03_create", raw)
    sim_id = body["simulation_id"]
    graph_id = body.get("graph_id")
    print(f"  simulation_id = {sim_id}, graph_id = {graph_id}")

    # 4. Prepare
    body = post_json(s, f"{base}/api/simulation/prepare",
                     {"simulation_id": sim_id, "use_llm_for_profiles": True,
                      "parallel_profile_count": 3},
                     "04_prepare_kick", raw)
    if not body.get("already_prepared"):
        poll("prepare",
             lambda: get_json(s, f"{base}/api/simulation/{sim_id}",
                              "04_prepare_state_latest", raw),
             interval=15, timeout=args.prepare_timeout,
             success=lambda b: (b.get("config_generated")
                                and b.get("profiles_count", 0) > 0),
             fail=lambda b: b.get("error"))
    print("  profiles ready")

    # 4.5. Locate sim_dir
    sim_dir = _detect_sim_dir(args.backend, sim_id, hint=args.sim_dir_hint)
    if not sim_dir or not sim_dir.exists():
        print(f"[FAIL] sim_dir not accessible for {sim_id}. "
              f"Provide --sim-dir-hint or MIROFISH_SIM_ROOT env var.")
        sys.exit(2)
    print(f"  sim_dir: {sim_dir}")

    # 5. Inject synthetic agents (or skip in mirofish mode)
    print(f"\n--- synthetic_agents (mode={args.mode}) ---")
    here = Path(__file__).parent
    cmd = [sys.executable, str(here / "synthetic_agents.py"),
           "--briefing", args.doc,
           "--preflight", args.preflight,
           "--sim-dir", str(sim_dir),
           "--out", str(out / "synthetic_agents.json"),
           "--mode", args.mode,
           "--llm-base-url", args.llm_base_url,
           "--llm-api-key", args.llm_api_key,
           "--llm-model", args.llm_model]
    rc = subprocess.call(cmd)
    if rc != 0:
        print(f"[FAIL] synthetic_agents.py exit {rc}")
        sys.exit(rc)

    # 6. Start (force=True since we changed agent count → DB needs reset)
    body = post_json(s, f"{base}/api/simulation/start",
                     {"simulation_id": sim_id,
                      "platform": args.platform,
                      "max_rounds": args.max_rounds,
                      "force": True},
                     "05_start", raw)
    print(f"  runner: {body.get('runner_status')} "
          f"(max_rounds={args.max_rounds}, platform={args.platform})")

    # 7. Monitor (plateau-aware)
    #
    # OASIS sometimes freezes mid-run: workers post a partial batch then the
    # round-counter stops advancing and `twitter_completed` never flips. The
    # vanilla poll() then waits the full --run-timeout (~5400s) before giving
    # up. To recover, we ALSO poll the posts table and treat a long plateau in
    # row count as a "soft completion" — artifacts already pulled exist and
    # downstream steps can succeed.
    plat = args.platform if args.platform != "parallel" else "twitter"
    plateau_threshold = int(os.environ.get("CRUCIBLE_PLATEAU_S", "180"))
    monitor_result = monitor_run(
        s, base, sim_id,
        platform=plat,
        run_timeout=args.run_timeout,
        plateau_threshold=plateau_threshold,
        wait_full_completion=(args.platform == "parallel"),
        raw_dir=raw,
    ) or {}
    print("  run completed (env left alive for interviews)")

    # 8. Pull artifacts (no close-env, no report — crucible runs its own reports)
    plat = args.platform if args.platform != "parallel" else "twitter"
    art_paths = [
        ("config", f"/api/simulation/{sim_id}/config"),
        ("profiles", f"/api/simulation/{sim_id}/profiles/realtime?platform={plat}"),
        ("graph_data", f"/api/graph/data/{graph_id}"),
        ("posts", f"/api/simulation/{sim_id}/posts?platform={plat}&limit=2000"),
        ("comments", f"/api/simulation/{sim_id}/comments?platform={plat}&limit=2000"),
        ("timeline", f"/api/simulation/{sim_id}/timeline?platform={plat}"),
        ("actions", f"/api/simulation/{sim_id}/actions?platform={plat}&limit=2000"),
        ("agent_stats", f"/api/simulation/{sim_id}/agent-stats?platform={plat}"),
    ]
    for label, path in art_paths:
        get_json(s, f"{base}{path}", f"art_{label}", raw)

    # 8.5 representation_metrics: who actually posted vs who could have.
    metrics = _compute_representation_metrics(out, monitor_result)
    (out / "representation_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2))
    if metrics.get("error"):
        print(f"[WARN] representation_metrics: {metrics['error']} "
              f"(missing={metrics.get('missing')})")
    else:
        print(f"  representation: {metrics['real_posts']}/{metrics['total_posts']} "
              f"posts from real ({metrics['real_post_ratio']:.0%}); "
              f"{metrics['silent_real_agents']}/{metrics['total_real_agents']} "
              f"real agents silent ({metrics['silent_real_ratio']:.0%}); "
              f"degraded_real_silent={metrics['degraded_real_silent']}")

    # 9. Manifest
    manifest = {
        "simulation_id": sim_id,
        "project_id": project_id,
        "graph_id": graph_id,
        "mode": args.mode,
        "platform": args.platform,
        "max_rounds": args.max_rounds,
        "doc": str(doc),
        "requirement": args.requirement,
        "sim_dir": str(sim_dir),
        "llm_base_url": args.llm_base_url,
        "llm_model": args.llm_model,
        "artifact_pull_limits": {"posts": 2000, "comments": 2000, "actions": 2000},
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"\nDONE. sim_id={sim_id}, sim_dir={sim_dir}")
    print(f"     artifacts: {raw}")
    print(f"     manifest:  {out}/manifest.json")


if __name__ == "__main__":
    main()

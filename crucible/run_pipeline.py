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

    # 7. Monitor
    poll("run",
         lambda: get_json(s, f"{base}/api/simulation/{sim_id}/run-status",
                          "06_run_status_latest", raw),
         interval=20, timeout=args.run_timeout,
         success=lambda b: (
             b.get("runner_status") in ("completed", "finished", "done")
             or (args.platform == "twitter" and b.get("twitter_completed"))
             or (args.platform == "reddit" and b.get("reddit_completed"))
             or (args.platform == "parallel"
                 and b.get("twitter_completed") and b.get("reddit_completed"))
         ),
         fail=lambda b: b.get("error") if b.get("runner_status") in ("failed", "error") else None)
    print("  run completed (env left alive for interviews)")

    # 8. Pull artifacts (no close-env, no report — crucible runs its own reports)
    plat = args.platform if args.platform != "parallel" else "twitter"
    art_paths = [
        ("config", f"/api/simulation/{sim_id}/config"),
        ("profiles", f"/api/simulation/{sim_id}/profiles/realtime?platform={plat}"),
        ("graph_data", f"/api/graph/data/{graph_id}"),
        ("posts", f"/api/simulation/{sim_id}/posts?platform={plat}&limit=500"),
        ("comments", f"/api/simulation/{sim_id}/comments?platform={plat}&limit=500"),
        ("timeline", f"/api/simulation/{sim_id}/timeline?platform={plat}"),
        ("actions", f"/api/simulation/{sim_id}/actions?platform={plat}&limit=2000"),
        ("agent_stats", f"/api/simulation/{sim_id}/agent-stats?platform={plat}"),
    ]
    for label, path in art_paths:
        get_json(s, f"{base}{path}", f"art_{label}", raw)

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
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"\nDONE. sim_id={sim_id}, sim_dir={sim_dir}")
    print(f"     artifacts: {raw}")
    print(f"     manifest:  {out}/manifest.json")


if __name__ == "__main__":
    main()

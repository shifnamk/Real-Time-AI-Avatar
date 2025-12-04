import os
from dotenv import load_dotenv
import json
import uuid
import time
from functools import wraps
import re
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, g, Response
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import secrets
from typing import Any, Dict
from datetime import datetime
import threading
import asyncio
import requests
import subprocess
import sys
import uuid as _uuid
from livekit import api as lk_api

try:
    # LiveKit webhook verification (optional, enabled if env vars present)
    from livekit.api import WebhookReceiver as LKWebhookReceiver
except Exception:
    LKWebhookReceiver = None
try:
    from flask_cors import CORS
except Exception:
    CORS = None  # Will handle gracefully if package missing
try:
    from twilio.twiml.voice_response import VoiceResponse
    from twilio.request_validator import RequestValidator as TwilioRequestValidator
except Exception:
    VoiceResponse = None
    TwilioRequestValidator = None

# Flask app must be defined before using @app.* decorators
app = Flask(__name__)
# Load environment from .env if present (allows LIVEKIT_* without exporting globally)
try:
    load_dotenv()
except Exception:
    pass

# Suppress noisy endpoint logging
import logging
class EndpointFilter(logging.Filter):
    def filter(self, record):
        # Suppress GET /numbers logs
        if hasattr(record, 'getMessage'):
            msg = record.getMessage()
            if 'GET /numbers' in msg or 'GET /api/numbers' in msg:
                return False
        return True

# Apply filter to werkzeug logger
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.addFilter(EndpointFilter())

# Enable permissive CORS for local development (React at :3000)
if CORS is not None:
    CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)
else:
    # Fallback minimal CORS headers if extension missing
    @app.after_request
    def _cors_headers(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        return resp

# Simple reverse-proxy friendly prefix for production deployments
API_PREFIX = os.getenv("API_PREFIX", "/api")

# KB MCP server configuration (optional). When set, backend can proxy UI calls to KB tools.
KB_MCP_URL = os.getenv("KB_MCP_URL", "").strip()
KB_AUTH_TOKEN = os.getenv("KB_AUTH_TOKEN", "").strip()

# Feature flag: control whether webhook should auto-spawn agent workers.
# When using LiveKit Cloud dispatch rules for named workers, set WEBHOOK_AUTOSPAWN=0
# to avoid duplicate workers joining the same room.
ENABLE_WEBHOOK_AUTOSPAWN = (os.getenv("WEBHOOK_AUTOSPAWN", "1") == "1")

# ------------------ EMBED SESSION STORE (EPHEMERAL) ------------------
# sid -> { agent_id, room, title, theme, created_at, exp }
_EMBED_SESSIONS: dict[str, dict] = {}
_EMBED_TTL_SECONDS_DEFAULT = int(os.getenv("EMBED_TTL_SECONDS", "900"))  # 15 minutes

# ------------------ PLAYGROUND TOOLS CACHE (EPHEMERAL) ------------------
# room -> [tool_schemas]
_PLAYGROUND_TOOLS: dict[str, list[dict]] = {}

def _embed_create_session(agent_id: str, room: str, title: str | None = None, theme: str | None = None, ttl_seconds: int | None = None) -> dict:
    sid = _uuid.uuid4().hex
    now = int(time.time())
    exp = now + int(ttl_seconds or _EMBED_TTL_SECONDS_DEFAULT)
    rec = {
        "sid": sid,
        "agent_id": agent_id,
        "room": room,
        "title": (title or ""),
        "theme": (theme or ""),
        "created_at": now,
        "exp": exp,
        "uses": 0,
    }
    _EMBED_SESSIONS[sid] = rec
    return rec

def _embed_get_session(sid: str) -> dict | None:
    try:
        rec = _EMBED_SESSIONS.get(str(sid))
        if not rec:
            return None
        if int(time.time()) > int(rec.get("exp", 0)):
            try:
                del _EMBED_SESSIONS[str(sid)]
            except Exception:
                pass
            return None
        return rec
    except Exception:
        return None

def _start_embed_gc_thread():
    def _gc():
        while True:
            try:
                now = int(time.time())
                for k, v in list(_EMBED_SESSIONS.items()):
                    try:
                        if now > int(v.get("exp", 0)):
                            _EMBED_SESSIONS.pop(k, None)
                    except Exception:
                        _EMBED_SESSIONS.pop(k, None)
            except Exception:
                pass
            time.sleep(30)
    try:
        t = threading.Thread(target=_gc, daemon=True)
        t.start()
    except Exception:
        pass

_start_embed_gc_thread()

def _playground_set_tools(room: str, tools: list[dict]) -> None:
    """Store tools for a playground session keyed by room name."""
    _PLAYGROUND_TOOLS[str(room)] = tools or []

def _playground_get_tools(room: str) -> list[dict]:
    """Retrieve tools for a room."""
    return _PLAYGROUND_TOOLS.get(str(room), [])

def _playground_clear_tools(room: str) -> None:
    """Clear tools for a room after they're loaded."""
    _PLAYGROUND_TOOLS.pop(str(room), None)

def _agents_dir() -> Path:
    return Path(os.getenv("AGENTS_DIR", Path(__file__).parent / "agents"))

def _ensure_storage() -> None:
    ad = _agents_dir()
    ad.mkdir(parents=True, exist_ok=True)
    agents_file = ad / "agents.json"
    if not agents_file.exists():
        agents_file.write_text(json.dumps({"agents": [], "active_agent_id": None}, indent=2), encoding="utf-8")
    active_file = ad / "active.json"
    if not active_file.exists():
        # store per-user active agents: { user_id: { ...agent } }
        active_file.write_text(json.dumps({}, indent=2), encoding="utf-8")
    users_file = ad / "users.json"
    if not users_file.exists():
        users_file.write_text(json.dumps({"users": []}, indent=2), encoding="utf-8")
    sessions_file = ad / "sessions.json"
    if not sessions_file.exists():
        sessions_file.write_text(json.dumps({"sessions": {}}, indent=2), encoding="utf-8")
    # legacy usage/subscription storage removed
    # Ensure upload directories
    uploads_dir = Path(__file__).parent
    # Background audio storage removed
    (uploads_dir / "transcripts").mkdir(parents=True, exist_ok=True)
    (uploads_dir / "greetings").mkdir(parents=True, exist_ok=True)
    # Per-number routing config for LiveKit trunks
    numbers_file = ad / "numbers.json"
    if not numbers_file.exists():
        numbers_file.write_text(json.dumps({"routes": {}}, indent=2), encoding="utf-8")
    # Per-user Twilio accounts and numbers (multi-tenant Twilio)
    twilio_file = ad / "twilio_accounts.json"
    if not twilio_file.exists():
        twilio_file.write_text(json.dumps({"accounts": [], "numbers": {}}, indent=2), encoding="utf-8")

def _can_access_agent(user: dict, agent: dict) -> bool:
    try:
        if not isinstance(user, dict) or not isinstance(agent, dict):
            return False
        if user.get("role") == "admin":
            return True
        return agent.get("owner_id") == user.get("id")
    except Exception:
        return False

def _get_agent(agent_id: str) -> dict | None:
    data = _load_all()
    for a in data.get("agents", []):
        if a.get("id") == agent_id:
            return a
    return None

def _resolve_kb_server(agent: dict) -> tuple[str | None, dict | None]:
    """Find a KB MCP server entry in the agent's MCP servers or fall back to env.

    Returns (url, headers) or (None, None) if not found.
    """
    try:
        # Prefer agent-specific MCP servers
        servers = ((agent.get("mcp") or {}).get("servers") or []) if isinstance(agent, dict) else []
        for s in servers:
            try:
                nm = str((s or {}).get("name") or "").strip().lower()
                url = str((s or {}).get("url") or "").strip()
                if not url:
                    continue
                # Consider entries explicitly named Knowledge Base or any /mcp endpoint
                if nm == "knowledge base" or url.lower().rstrip("/").endswith("mcp"):
                    headers = (s or {}).get("headers") if isinstance((s or {}).get("headers"), dict) else None
                    return url, headers
            except Exception:
                continue
    except Exception:
        pass
    # Fallback to global env configuration
    if KB_MCP_URL:
        hdrs = {"Authorization": f"Bearer {KB_AUTH_TOKEN}"} if KB_AUTH_TOKEN else None
        return KB_MCP_URL, hdrs
    return None, None

def _synthesize_greeting_async(agent_obj: dict, agent_id: str) -> None:
    """Generate greeting audio for agent_obj in a background thread.

    Writes an audio file to greetings/{agent_id}.(mp3|opus) and updates
    agents.json and active.json with assets.greeting_audio when complete.
    """
    try:
        greeting_text = ((agent_obj or {}).get("greeting", {}) or {}).get("text") or ""
        if not greeting_text:
            return

        # Determine whether to use OpenAI realtime voice or Fish TTS
        realtime_cfg = (agent_obj or {}).get("realtime", {}) if isinstance((agent_obj or {}).get("realtime"), dict) else {}
        use_openai_voice = bool(realtime_cfg.get("enabled")) and str(realtime_cfg.get("tts_provider", "openai")).lower() == "openai"

        # Paths helper
        def _out_paths(fmt: str) -> tuple[str, Path]:
            rel = f"greetings/{agent_id}.{fmt}"
            return rel, Path(__file__).parent / rel

        def _persist_assets(primary_rel: str, preview_rel: str | None = None) -> None:
            # Update agents.json
            try:
                data = _load_all()
                changed = False
                for i, a in enumerate(data.get("agents", [])):
                    if a.get("id") == agent_id:
                        a.setdefault("assets", {})["greeting_audio"] = primary_rel
                        if preview_rel:
                            a["assets"]["greeting_audio_preview"] = preview_rel
                        data["agents"][i] = a
                        changed = True
                        break
                if changed:
                    _save_all(data)
            except Exception:
                pass
            # Update active.json entries
            try:
                act_p = _agents_dir() / "active.json"
                amap = json.loads(act_p.read_text(encoding="utf-8"))
                for uid, prof in list(amap.items()):
                    if isinstance(prof, dict) and (prof.get("id") == agent_id or (agent_id == "active")):
                        prof.setdefault("assets", {})["greeting_audio"] = primary_rel
                        if preview_rel:
                            prof["assets"]["greeting_audio_preview"] = preview_rel
                        amap[uid] = prof
                act_p.write_text(json.dumps(amap, indent=2), encoding="utf-8")
            except Exception:
                pass

        def _worker_openai() -> None:
            try:
                # Resolve OpenAI API key/base_url and voice
                d = _load_defaults()
                openai_key = os.getenv("OPENAI_API_KEY") or ((d.get("openai", {}) or {}).get("api_key") if isinstance(d, dict) else None) or ""
                base_url = ((d.get("openai", {}) or {}).get("base_url") if isinstance(d, dict) else None) or None
                voice = realtime_cfg.get("voice") or ((d.get("realtime", {}) or {}).get("voice") if isinstance(d, dict) else None) or "verse"
                if not openai_key:
                    # Without a key, skip to Fish TTS fallback
                    _worker_fish()
                    return
                from openai import OpenAI  # type: ignore
                client = OpenAI(api_key=openai_key, base_url=base_url) if base_url else OpenAI(api_key=openai_key)

                # 1) Generate WAV for call playout (widely supported by decoder)
                wav_rel, wav_abs = _out_paths("wav")
                try:
                    with client.audio.speech.with_streaming_response.create(
                        model="gpt-4o-mini-tts",
                        voice=str(voice or "verse"),
                        input=greeting_text,
                        format="wav",
                    ) as resp:
                        resp.stream_to_file(str(wav_abs))
                except Exception:
                    audio_resp = client.audio.speech.create(
                        model="gpt-4o-mini-tts",
                        voice=str(voice or "verse"),
                        input=greeting_text,
                        format="wav",
                    )
                    try:
                        with open(wav_abs, "wb") as f:
                            f.write(audio_resp.content if hasattr(audio_resp, "content") else audio_resp)  # type: ignore
                    except Exception:
                        pass

                # 2) Generate MP3 for UI preview
                mp3_rel, mp3_abs = _out_paths("mp3")
                try:
                    with client.audio.speech.with_streaming_response.create(
                        model="gpt-4o-mini-tts",
                        voice=str(voice or "verse"),
                        input=greeting_text,
                        format="mp3",
                    ) as resp:
                        resp.stream_to_file(str(mp3_abs))
                except Exception:
                    audio_resp2 = client.audio.speech.create(
                        model="gpt-4o-mini-tts",
                        voice=str(voice or "verse"),
                        input=greeting_text,
                        format="mp3",
                    )
                    try:
                        with open(mp3_abs, "wb") as f:
                            f.write(audio_resp2.content if hasattr(audio_resp2, "content") else audio_resp2)  # type: ignore
                    except Exception:
                        pass

                _persist_assets(wav_rel, mp3_rel)
            except Exception:
                # If OpenAI path fails for any reason, try Fish as a fallback
                try:
                    _worker_fish()
                except Exception:
                    pass

        def _worker_fish() -> None:
            try:
                tts_cfg = (agent_obj or {}).get("tts", {}) if isinstance((agent_obj or {}).get("tts"), dict) else {}
                voice_id = tts_cfg.get("reference_id") or os.getenv("DEFAULT_VOICE", "")
                api_key = tts_cfg.get("api_key") or os.getenv("FISH_AUDIO_API_KEY", "")
                if not (voice_id and api_key):
                    return
                from fish_tts_provider import FishSpeechTTS
                tts = FishSpeechTTS(
                    api_key=api_key,
                    reference_id=voice_id,
                    model=tts_cfg.get("model", os.getenv("FISH_AUDIO_MODEL", "s1")),
                    response_format="opus",
                    latency=tts_cfg.get("latency", os.getenv("FISH_AUDIO_LATENCY", "low")),
                    temperature=float(tts_cfg.get("temperature", os.getenv("FISH_AUDIO_TEMPERATURE", "0.7"))),
                    top_p=float(tts_cfg.get("top_p", os.getenv("FISH_AUDIO_TOP_P", "0.7"))),
                    speed=float(tts_cfg.get("speed", os.getenv("FISH_AUDIO_SPEED", "1.0"))),
                    volume_db=float(tts_cfg.get("volume_db", os.getenv("FISH_AUDIO_VOLUME_DB", "0.0"))),
                    frame_size_ms=int(tts_cfg.get("frame_size_ms", 40)),
                    emotion_prefix=(tts_cfg.get("emotion_prefix") or None),
                )
                # OPUS for calls
                opus_rel, opus_abs = _out_paths("opus")
                asyncio.run(tts.synthesize_to_file(greeting_text, str(opus_abs)))
                # MP3 for preview
                try:
                    tts_mp3 = FishSpeechTTS(
                        api_key=api_key,
                        reference_id=voice_id,
                        model=tts_cfg.get("model", os.getenv("FISH_AUDIO_MODEL", "s1")),
                        response_format="mp3",
                        latency=tts_cfg.get("latency", os.getenv("FISH_AUDIO_LATENCY", "low")),
                        temperature=float(tts_cfg.get("temperature", os.getenv("FISH_AUDIO_TEMPERATURE", "0.7"))),
                        top_p=float(tts_cfg.get("top_p", os.getenv("FISH_AUDIO_TOP_P", "0.7"))),
                        speed=float(tts_cfg.get("speed", os.getenv("FISH_AUDIO_SPEED", "1.0"))),
                        volume_db=float(tts_cfg.get("volume_db", os.getenv("FISH_AUDIO_VOLUME_DB", "0.0"))),
                        frame_size_ms=int(tts_cfg.get("frame_size_ms", 40)),
                        emotion_prefix=(tts_cfg.get("emotion_prefix") or None),
                    )
                    mp3_rel, mp3_abs = _out_paths("mp3")
                    asyncio.run(tts_mp3.synthesize_to_file(greeting_text, str(mp3_abs)))
                except Exception:
                    mp3_rel = None
                _persist_assets(opus_rel, mp3_rel)
            except Exception:
                # best-effort
                pass

        # Dispatch to selected provider
        if use_openai_voice:
            threading.Thread(target=_worker_openai, daemon=True).start()
        else:
            threading.Thread(target=_worker_fish, daemon=True).start()
    except Exception:
        pass

def _load_all():
    _ensure_storage()
    with open(_agents_dir() / "agents.json", "r", encoding="utf-8") as f:
        return json.load(f)

def _numbers_path() -> Path:
    return _agents_dir() / "numbers.json"

def _load_numbers() -> dict:
    _ensure_storage()
    try:
        return json.loads(_numbers_path().read_text(encoding="utf-8"))
    except Exception:
        return {"routes": {}}

def _save_numbers(data: dict) -> None:
    _numbers_path().write_text(json.dumps(data or {"routes": {}}, indent=2), encoding="utf-8")

def _save_all(data):
    with open(_agents_dir() / "agents.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# ------------------ TWILIO MULTI-TENANT STORAGE ------------------
def _twilio_store_path() -> Path:
    return _agents_dir() / "twilio_accounts.json"

def _load_twilio_store() -> dict:
    _ensure_storage()
    try:
        return json.loads(_twilio_store_path().read_text(encoding="utf-8"))
    except Exception:
        return {"accounts": [], "numbers": {}}

def _save_twilio_store(data: dict) -> None:
    _twilio_store_path().write_text(json.dumps(data or {"accounts": [], "numbers": {}}, indent=2), encoding="utf-8")

def _digits_only(raw: str) -> str:
    return re.sub(r"[^0-9]", "", str(raw or ""))

def _normalize_e164(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return s
    d = _digits_only(s)
    if s.startswith("+"):
        return "+" + d
    # Assume US default if 10 digits; otherwise just return digits
    if len(d) == 10:
        return "+1" + d
    if d:
        return "+" + d if not s.startswith("+") else s
    return s

def _twilio_get_accounts_for_user(user_id: str) -> list[dict]:
    store = _load_twilio_store()
    out = []
    for acc in store.get("accounts", []):
        try:
            if acc.get("user_id") == user_id:
                out.append(acc)
        except Exception:
            continue
    return out

def _twilio_get_account_by_id(account_id: str) -> dict | None:
    store = _load_twilio_store()
    for acc in store.get("accounts", []):
        if acc.get("id") == account_id:
            return acc
    return None

def _twilio_auth_token_for_number(number: str) -> str | None:
    """Resolve the Twilio auth token for the owner of a given phone number."""
    try:
        num_key = _normalize_e164(number)
        store = _load_twilio_store()
        entry = (store.get("numbers", {}) or {}).get(num_key)
        if not entry:
            # also try digits-only key for resilience
            entry = (store.get("numbers", {}) or {}).get(_digits_only(num_key))
        if entry:
            acc_id = entry.get("account_id")
            if acc_id:
                acc = _twilio_get_account_by_id(acc_id)
                if acc:
                    return acc.get("auth_token")
    except Exception:
        pass
    return None

def _load_routes_union() -> dict:
    """Merge global numbers.json routes with per-user Twilio numbers mapping.
    Per-user mappings take precedence on conflict.
    Returns { number: { agent_id, trunk, user_id?, account_id? } }
    """
    routes = {}
    try:
        gcur = (_load_numbers() or {}).get("routes", {})
        for k, v in (gcur or {}).items():
            routes[str(k)] = {"agent_id": (v or {}).get("agent_id"), "trunk": (v or {}).get("trunk")}
    except Exception:
        pass
    try:
        store = _load_twilio_store() or {}
        for k, v in (store.get("numbers") or {}).items():
            try:
                routes[str(k)] = {
                    "agent_id": (v or {}).get("agent_id"),
                    "trunk": (v or {}).get("trunk"),
                    "user_id": (v or {}).get("user_id"),
                    "account_id": (v or {}).get("account_id"),
                }
            except Exception:
                continue
    except Exception:
        pass
    return routes

def _looks_like_agent_profile(obj: dict) -> bool:
    return isinstance(obj, dict) and (
        ("name" in obj and "instructions" in obj)
        or ("llm" in obj and "tts" in obj)
        or ("id" in obj and ("greeting" in obj or "behavior" in obj))
    )

def _set_active_global(agent: dict | None) -> None:
    """Store a single global active agent profile in active.json."""
    _ensure_storage()
    active_path = _agents_dir() / "active.json"
    if agent is None:
        active_path.write_text(json.dumps({}, indent=2), encoding="utf-8")
    else:
        active_path.write_text(json.dumps(agent, indent=2), encoding="utf-8")

def _load_active_global() -> dict:
    """Load the single global active agent. If file contains a legacy per-user map,
    return the first agent-shaped value.
    """
    _ensure_storage()
    active_path = _agents_dir() / "active.json"
    try:
        raw = json.loads(active_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if _looks_like_agent_profile(raw):
        return raw
    if isinstance(raw, dict) and raw:
        # Legacy map: { user_id: { ...agent } } → pick first agent-like value
        for v in raw.values():
            if isinstance(v, dict):
                return v
    return {}

def _set_active_for_user(user_id: str, agent: dict | None):
    # Backward-compatible wrapper: now we use a single global active agent
    _set_active_global(agent)

def _load_active_for_user(user_id: str):
    # Backward-compatible wrapper: load the single global active agent
    return _load_active_global()

def _users_path() -> Path:
    return _agents_dir() / "users.json"

def _sessions_path() -> Path:
    return _agents_dir() / "sessions.json"

# ------------------ DEFAULTS (GLOBAL) ------------------
def _defaults_path() -> Path:
    return _agents_dir() / "default.json"

def _load_defaults() -> dict:
    _ensure_storage()
    p = _defaults_path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    # Base defaults if file missing/corrupt
    return {"llm": {}, "stt": {}, "tts": {}, "realtime": {}, "livekit": {}, "openai": {}, "env": {}}

def _save_defaults(data: dict) -> None:
    try:
        _defaults_path().write_text(json.dumps(data or {}, indent=2), encoding="utf-8")
    except Exception:
        pass

# ------------------ VOICES STORAGE ------------------
def _voices_path() -> Path:
    return _agents_dir() / "voices.json"

def _ensure_voices_file() -> None:
    _ensure_storage()
    p = _voices_path()
    if not p.exists():
        p.write_text(json.dumps({"voices": []}, indent=2), encoding="utf-8")

def _load_voices() -> dict:
    _ensure_voices_file()
    try:
        return json.loads(_voices_path().read_text(encoding="utf-8"))
    except Exception:
        return {"voices": []}

def _save_voices(data: dict) -> None:
    _voices_path().write_text(json.dumps(data, indent=2), encoding="utf-8")

# ------------------ LIVEKIT WEBHOOK STORAGE ------------------
def _livekit_store_path() -> Path:
    return _agents_dir() / "livekit_store.json"

def _load_livekit_store() -> dict:
    _ensure_storage()
    p = _livekit_store_path()
    if not p.exists():
        p.write_text(json.dumps({"calls": {}}, indent=2), encoding="utf-8")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"calls": {}}

def _save_livekit_store(data: dict) -> None:
    _livekit_store_path().write_text(json.dumps(data, indent=2), encoding="utf-8")

def _load_users() -> dict:
    _ensure_storage()
    return json.loads(_users_path().read_text(encoding="utf-8"))

def _save_users(data: dict) -> None:
    _users_path().write_text(json.dumps(data, indent=2), encoding="utf-8")

def _get_user_by_id(user_id: str) -> dict | None:
    try:
        for u in _load_users().get("users", []):
            if u.get("id") == user_id:
                return u
    except Exception:
        return None
    return None

def _load_sessions() -> dict:
    _ensure_storage()
    return json.loads(_sessions_path().read_text(encoding="utf-8"))

def _save_sessions(data: dict) -> None:
    _sessions_path().write_text(json.dumps(data, indent=2), encoding="utf-8")

def _create_reset_token(user_id: str, ttl_seconds: int = 900) -> str:
    sess = _load_sessions()
    token = uuid.uuid4().hex
    resets = sess.setdefault("resets", {})
    resets[token] = {"user_id": user_id, "exp": int(time.time()) + ttl_seconds}
    _save_sessions(sess)
    return token

def _consume_reset_token(token: str) -> str | None:
    sess = _load_sessions()
    entry = sess.get("resets", {}).get(token)
    if not entry:
        return None
    if int(time.time()) > int(entry.get("exp", 0)):
        # expired
        del sess["resets"][token]
        _save_sessions(sess)
        return None
    user_id = entry.get("user_id")
    del sess["resets"][token]
    _save_sessions(sess)
    return user_id

def _load_usage() -> dict:
    _ensure_storage()
    return json.loads(_usage_path().read_text(encoding="utf-8"))

def _save_usage(data: dict) -> None:
    _usage_path().write_text(json.dumps(data, indent=2), encoding="utf-8")

def _load_subs() -> dict:
    _ensure_storage()
    return json.loads(_subs_path().read_text(encoding="utf-8"))

def _save_subs(data: dict) -> None:
    _subs_path().write_text(json.dumps(data, indent=2), encoding="utf-8")

def _issue_token(user_id: str) -> str:
    sessions = _load_sessions()
    token = uuid.uuid4().hex
    sessions.setdefault("sessions", {})[token] = {"user_id": user_id, "issued_at": int(time.time())}
    _save_sessions(sessions)
    return token

def _get_user_by_token(token: str) -> dict | None:
    if not token:
        return None
    sessions = _load_sessions().get("sessions", {})
    entry = sessions.get(token)
    if not entry:
        return None
    users = _load_users().get("users", [])
    for u in users:
        if u.get("id") == entry.get("user_id"):
            return u
    return None

def require_auth(admin: bool = False):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            auth = request.headers.get("Authorization", "")
            token = auth.split(" ")[-1] if auth.startswith("Bearer ") else auth
            user = _get_user_by_token(token)
            if not user:
                return jsonify({"error": "unauthorized"}), 401
            if admin and user.get("role") != "admin":
                return jsonify({"error": "forbidden"}), 403
            g.current_user = user
            return fn(*args, **kwargs)
        return wrapper
    return decorator

def require_internal():
    """Require an internal shared secret for backend-only endpoints.

    Checks header X-Internal-Key against env INTERNAL_API_KEY. Returns 403
    when missing/mismatched. Returns 500 if not configured to avoid
    accidentally exposing the endpoint without protection.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            expected = os.getenv("INTERNAL_API_KEY", "")
            if not expected:
                return jsonify({"error": "internal_key_not_configured"}), 500
            provided = request.headers.get("X-Internal-Key") or request.headers.get("x-internal-key")
            if str(provided or "") != str(expected):
                return jsonify({"error": "forbidden"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator

def _internal_key_matches() -> bool:
    try:
        expected = os.getenv("INTERNAL_API_KEY", "")
        if not expected:
            return False
        provided = request.headers.get("X-Internal-Key") or request.headers.get("x-internal-key")
        return str(provided or "") == str(expected)
    except Exception:
        return False

"""Legacy plan/usage/subscription logic fully removed."""

# ------------------ EMBED API ------------------
@app.post("/embed/create")
@require_auth()
def embed_create():
    """Create an embeddable session and return an iframe URL.
    Payload: { agent_id: string, room?: string, title?: string, theme?: string, ttl_seconds?: number }
    Response: { sid, room, iframe_url, expires_at }
    """
    payload = request.json or {}
    agent_id = (payload.get("agent_id") or "").strip()
    if not agent_id:
        return jsonify({"error": "agent_id_required"}), 400
    room = (payload.get("room") or f"embed-{_uuid.uuid4().hex[:8]}").strip()
    title = (payload.get("title") or "").strip()
    theme = (payload.get("theme") or "").strip()
    ttl_seconds = payload.get("ttl_seconds")
    rec = _embed_create_session(agent_id, room, title, theme, ttl_seconds)
    # Build iframe URL on this host
    try:
        base = (os.getenv("PUBLIC_BASE_URL") or request.host_url).rstrip("/")
    except Exception:
        base = (request.host_url if hasattr(request, "host_url") else "").rstrip("/")
    iframe_url = f"{base}/embed/widget?sid={rec['sid']}"
    return jsonify({
        "sid": rec["sid"],
        "room": room,
        "iframe_url": iframe_url,
        "expires_at": int(rec["exp"]),
    })

@app.get("/embed/token/<sid>")
def embed_token(sid: str):
    """Issue a LiveKit token for the embed widget and spawn the agent in background if needed.

    This endpoint is intentionally unauthenticated; possession of the time-limited sid is required.
    Optionally restrict by EMBED_ALLOWED_ORIGINS (comma-separated exact origins).
    """
    # Optional Origin allowlist
    try:
        allow = [x.strip() for x in str(os.getenv("EMBED_ALLOWED_ORIGINS", "")).split(",") if x.strip()]
        if allow:
            origin = request.headers.get("Origin") or ""
            if origin and origin not in allow:
                return jsonify({"error": "forbidden_origin"}), 403
    except Exception:
        pass

    rec = _embed_get_session(sid)
    if not rec:
        return jsonify({"error": "invalid_or_expired"}), 404

    # Resolve LiveKit config from agent-specific config or defaults
    try:
        data = _load_all()
        agent_conf = next((a for a in (data.get("agents") or []) if a.get("id") == rec.get("agent_id")), {})
    except Exception:
        agent_conf = {}
    d = _load_defaults()
    lk_cfg = (agent_conf or {}).get("livekit", {}) if isinstance(agent_conf, dict) else {}
    lk_def = (d or {}).get("livekit", {})
    lk_url = str(lk_cfg.get("url") or lk_def.get("url") or os.getenv("LIVEKIT_URL", ""))
    lk_key = str(lk_cfg.get("api_key") or lk_def.get("api_key") or os.getenv("LIVEKIT_API_KEY", ""))
    lk_secret = str(lk_cfg.get("api_secret") or lk_def.get("api_secret") or os.getenv("LIVEKIT_API_SECRET", ""))
    if not (lk_url and lk_key and lk_secret):
        return jsonify({"error": "missing_livekit_env"}), 500

    # Mint browser token
    identity = f"user-{_uuid.uuid4().hex[:6]}"
    try:
        token = lk_api.AccessToken(lk_key, lk_secret)
        token.with_identity(identity)
        token.with_name(identity)
        grants = lk_api.VideoGrants(
            room=rec["room"],
            room_join=True,
            can_publish=True,
            can_subscribe=True,
        )
        token.with_grants(grants)
        jwt = token.to_jwt()
    except Exception as e:
        return jsonify({"error": f"token_error: {e}"}), 500

    # Spawn agent once per session (best-effort)
    try:
        if not rec.get("spawned"):
            rec["spawned"] = True
            def _spawn() -> None:
                try:
                    env = os.environ.copy()
                    env.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", os.getenv("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python"))
                    env.setdefault("PYTHONUNBUFFERED", "1")
                    env["RUN_AS_WORKER"] = "1"
                    env["PLAYGROUND_ROOM_NAME"] = str(rec["room"]) 
                    env.setdefault("PLAYGROUND_IDENTITY", f"agent-{_uuid.uuid4().hex[:6]}")
                    env["AGENT_PROFILE_ID"] = str(rec["agent_id"]) 
                    env["LIVEKIT_URL"] = lk_url
                    env["LIVEKIT_API_KEY"] = lk_key
                    env["LIVEKIT_API_SECRET"] = lk_secret
                    try:
                        openai_def = (d or {}).get("openai", {})
                        if openai_def.get("api_key"):
                            env.setdefault("OPENAI_API_KEY", str(openai_def.get("api_key")))
                        if openai_def.get("base_url"):
                            env.setdefault("OPENAI_BASE_URL", str(openai_def.get("base_url")))
                    except Exception:
                        pass
                    env.setdefault("AGENT_NAME", f"embed-{rec['agent_id']}")
                    try:
                        env.setdefault("CONFIG_API_BASE", os.getenv("CONFIG_API_BASE", f"http://localhost:{os.getenv('CONFIG_API_PORT','5057')}"))
                    except Exception:
                        pass
                    agent_path = str((Path(__file__).parent / "agent.py").resolve())
                    try:
                        logs_dir = Path(__file__).parent / "logs"
                        logs_dir.mkdir(parents=True, exist_ok=True)
                        log_fp = open(str(logs_dir / f"embed_{rec['room']}.log"), "wb")
                    except Exception:
                        log_fp = None
                    if log_fp is not None:
                        proc = subprocess.Popen([sys.executable, agent_path], env=env, stdout=log_fp, stderr=subprocess.STDOUT)
                        _PLAYGROUND_LOGFILES[rec["room"]] = log_fp
                    else:
                        proc = subprocess.Popen([sys.executable, agent_path], env=env)
                    _PLAYGROUND_PROCS[rec["room"]] = proc
                except Exception:
                    pass
            try:
                threading.Thread(target=_spawn, daemon=True).start()
            except Exception:
                _spawn()
    except Exception:
        pass

    # Accounting
    try:
        rec["uses"] = int(rec.get("uses", 0)) + 1
    except Exception:
        pass

    return jsonify({
        "server_url": lk_url,
        "room": rec["room"],
        "token": jwt,
        "identity": identity,
        "title": rec.get("title") or "",
        "theme": rec.get("theme") or "",
    })

@app.get("/embed/widget")
def serve_embed_widget():
    """Serve the embeddable widget HTML. Requires ?sid=... in query."""
    base_dir = Path(__file__).parent / "embed"
    base_dir.mkdir(parents=True, exist_ok=True)
    try:
        return send_from_directory(str(base_dir), "widget.html")
    except Exception:
        # If file missing, return a minimal placeholder to avoid 404s
        html = (
            "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Agent Widget</title></head><body>Widget file not found.</body></html>"
        )
        return Response(html, mimetype="text/html")

@app.get("/embed/agents")
def serve_embed_agents():
    """Serve a minimal admin page to list and delete agents."""
    base_dir = Path(__file__).parent / "embed"
    base_dir.mkdir(parents=True, exist_ok=True)
    try:
        return send_from_directory(str(base_dir), "agents.html")
    except Exception:
        html = (
            "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Agents Admin</title></head><body>Page not found.</body></html>"
        )
        return Response(html, mimetype="text/html")

try:
    _duplicate_routes_with_prefix(app, API_PREFIX)
except Exception:
    # Function defined later; will be called again after definition.
    pass

def _deep_merge(dest: dict, src: dict) -> dict:
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dest.get(k), dict):
            dest[k] = _deep_merge(dest.get(k, {}), v)
        else:
            dest[k] = v
    return dest

# ------------------ MCP Introspection ------------------
@app.post("/mcp/introspect")
@require_auth(admin=True)
def mcp_introspect():
    """Introspect a remote MCP server and return its tool list.

    Payload: { url: string, headers?: object }
    Returns: { tools: [{ name, description, parameters }] }
    """
    payload = request.json or {}
    url = str(payload.get("url") or "").strip()
    headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else None
    if not url:
        return jsonify({"tools": [], "error": "url_required"}), 400
    try:
        from livekit.agents.llm.mcp import MCPServerHTTP  # type: ignore
        tools_out: list[dict] = []

        async def _run():
            srv = MCPServerHTTP(url=url, headers=headers)
            await srv.initialize()
            try:
                tools = await srv.list_tools()
                for t in tools:
                    try:
                        info = getattr(t, "__livekit_raw_tool_info", None)
                        if info and isinstance(info, object):
                            raw = getattr(info, "raw_schema", {}) or {}
                            tools_out.append({
                                "name": raw.get("name"),
                                "description": raw.get("description"),
                                "parameters": raw.get("parameters") or {},
                            })
                    except Exception:
                        pass
            finally:
                await srv.aclose()

        asyncio.run(_run())
        return jsonify({"tools": tools_out})
    except Exception as e:
        return jsonify({"tools": [], "error": str(e)}), 500

@app.get("/agents/<agent_id>/mcp/tools")
@require_auth(admin=True)
def mcp_tools_for_agent(agent_id: str):
    """Aggregate tools from all MCP servers configured on an agent."""
    try:
        data = _load_all()
        agent = next((a for a in data.get("agents", []) if a.get("id") == agent_id), None)
        if not agent:
            return jsonify({"tools": []}), 404
        servers = ((agent.get("mcp") or {}).get("servers") or []) if isinstance(agent, dict) else []
        tools_all: list[dict] = []
        from livekit.agents.llm.mcp import MCPServerHTTP  # type: ignore

        async def _introspect_one(s):
            url = str((s or {}).get("url") or "").strip()
            if not url or not (s or {}).get("enabled", True):
                return []
            headers = (s or {}).get("headers") if isinstance((s or {}).get("headers"), dict) else None
            srv = MCPServerHTTP(url=url, headers=headers)
            await srv.initialize()
            try:
                tools = await srv.list_tools()
                out = []
                for t in tools:
                    try:
                        info = getattr(t, "__livekit_raw_tool_info", None)
                        if info and isinstance(info, object):
                            raw = getattr(info, "raw_schema", {}) or {}
                            out.append({
                                "name": raw.get("name"),
                                "description": raw.get("description"),
                                "parameters": raw.get("parameters") or {},
                            })
                    except Exception:
                        continue
                return out
            finally:
                await srv.aclose()

        async def _run_all():
            results = await asyncio.gather(*(_introspect_one(s) for s in servers), return_exceptions=True)
            for arr in results:
                if isinstance(arr, BaseException):
                    continue
                tools_all.extend(arr or [])

        asyncio.run(_run_all())
        return jsonify({"tools": tools_all})
    except Exception as e:
        return jsonify({"tools": [], "error": str(e)}), 500

# ------------------ KB Proxies (optional) ------------------
@app.post("/agents/<agent_id>/kb/ingest")
@require_auth()
def kb_ingest_proxy(agent_id: str):
    agent = _get_agent(agent_id)
    if not agent or not _can_access_agent(g.current_user, agent):
        return jsonify({"error": "forbidden"}), 403
    payload = request.json or {}
    try:
        from livekit.agents.llm.mcp import MCPServerHTTP  # type: ignore

        async def _run():
            url, headers = _resolve_kb_server(agent)
            if not url:
                return {"error": "kb_server_unconfigured"}
            srv = MCPServerHTTP(url=url, headers=headers)
            await srv.initialize()
            try:
                tools = await srv.list_tools()
                tool = next((t for t in tools if getattr(getattr(t, "__livekit_raw_tool_info", None), "name", None) == "kb.ingest"), None)
                if tool is None:
                    return {"error": "tool_not_found"}
                args = {
                    "agent_id": agent_id,
                    "source": payload.get("source"),
                    "content": payload.get("content"),
                    "url": payload.get("url"),
                    "file_url": payload.get("file_url"),
                    "metadata": payload.get("metadata") or {},
                }
                res_json = await tool(args)  # type: ignore[call-arg]
                try:
                    return json.loads(res_json)
                except Exception:
                    return {"raw": res_json}
            finally:
                await srv.aclose()

        out = asyncio.run(_run())
        status = 200 if isinstance(out, dict) and not out.get("error") else 400
        return jsonify(out), status
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/agents/<agent_id>/kb/list")
@require_auth()
def kb_list_proxy(agent_id: str):
    agent = _get_agent(agent_id)
    if not agent or not _can_access_agent(g.current_user, agent):
        return jsonify({"error": "forbidden"}), 403
    limit = int(request.args.get("limit", "50"))
    cursor = request.args.get("cursor")
    try:
        from livekit.agents.llm.mcp import MCPServerHTTP  # type: ignore

        async def _run():
            url, headers = _resolve_kb_server(agent)
            if not url:
                return {"error": "kb_server_unconfigured"}
            srv = MCPServerHTTP(url=url, headers=headers)
            await srv.initialize()
            try:
                tools = await srv.list_tools()
                tool = next((t for t in tools if getattr(getattr(t, "__livekit_raw_tool_info", None), "name", None) == "kb.list"), None)
                if tool is None:
                    return {"error": "tool_not_found"}
                args = {"agent_id": agent_id, "limit": limit}
                if cursor:
                    args["cursor"] = cursor
                res_json = await tool(args)  # type: ignore[call-arg]
                try:
                    return json.loads(res_json)
                except Exception:
                    return {"raw": res_json}
            finally:
                await srv.aclose()

        out = asyncio.run(_run())
        status = 200 if isinstance(out, dict) and not out.get("error") else 400
        return jsonify(out), status
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/agents/<agent_id>/kb/stats")
@require_auth()
def kb_stats_proxy(agent_id: str):
    agent = _get_agent(agent_id)
    if not agent or not _can_access_agent(g.current_user, agent):
        return jsonify({"error": "forbidden"}), 403
    try:
        from livekit.agents.llm.mcp import MCPServerHTTP  # type: ignore

        async def _run():
            url, headers = _resolve_kb_server(agent)
            if not url:
                return {"error": "kb_server_unconfigured"}
            srv = MCPServerHTTP(url=url, headers=headers)
            await srv.initialize()
            try:
                tools = await srv.list_tools()
                tool = next((t for t in tools if getattr(getattr(t, "__livekit_raw_tool_info", None), "name", None) == "kb.stats"), None)
                if tool is None:
                    return {"error": "tool_not_found"}
                args = {"agent_id": agent_id}
                res_json = await tool(args)  # type: ignore[call-arg]
                try:
                    return json.loads(res_json)
                except Exception:
                    return {"raw": res_json}
            finally:
                await srv.aclose()

        out = asyncio.run(_run())
        status = 200 if isinstance(out, dict) and not out.get("error") else 400
        return jsonify(out), status
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.delete("/agents/<agent_id>/kb/<doc_id>")
@require_auth()
def kb_delete_proxy(agent_id: str, doc_id: str):
    agent = _get_agent(agent_id)
    if not agent or not _can_access_agent(g.current_user, agent):
        return jsonify({"error": "forbidden"}), 403
    try:
        from livekit.agents.llm.mcp import MCPServerHTTP  # type: ignore

        async def _run():
            url, headers = _resolve_kb_server(agent)
            if not url:
                return {"error": "kb_server_unconfigured"}
            srv = MCPServerHTTP(url=url, headers=headers)
            await srv.initialize()
            try:
                tools = await srv.list_tools()
                tool = next((t for t in tools if getattr(getattr(t, "__livekit_raw_tool_info", None), "name", None) == "kb.delete"), None)
                if tool is None:
                    return {"error": "tool_not_found"}
                args = {"agent_id": agent_id, "doc_id": doc_id}
                res_json = await tool(args)  # type: ignore[call-arg]
                try:
                    return json.loads(res_json)
                except Exception:
                    return {"raw": res_json}
            finally:
                await srv.aclose()

        out = asyncio.run(_run())
        status = 200 if isinstance(out, dict) and not out.get("error") else 400
        return jsonify(out), status
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/agents")
@require_auth()
def list_agents():
    user = g.current_user
    is_admin = user.get("role") == "admin"
    has_internal = _internal_key_matches()
    data = _load_all()
    # Return all agents to all authenticated users, but include non-sensitive owner info for display
    users = _load_users().get("users", [])
    uid_to_owner = {u.get("id"): {"id": u.get("id"), "username": u.get("username"), "email": u.get("email"), "role": u.get("role")} for u in users}
    enriched = []
    changed_any = False
    for a in data.get("agents", []):
        try:
            owner = uid_to_owner.get(a.get("owner_id"))
            # Hide non-admin-owned agents from non-admin users by default (UI).
            # If internal key is provided, show own agents plus admin-owned.
            if not is_admin:
                # Show admin-owned agents and the current user's own agents
                if not ((owner and owner.get("role") == "admin") or a.get("owner_id") == user.get("id")):
                    continue
            # Persistently remove deprecated fields from storage
            try:
                removed = False
                if "background_audio" in a:
                    del a["background_audio"]
                    removed = True
                if "language_policy" in a:
                    del a["language_policy"]
                    removed = True
                if removed:
                    changed_any = True
            except Exception:
                pass

            proj = {**a}
            # Mask MCP headers in list responses (never return tokens)
            try:
                mcp_cfg = (proj.get("mcp") or {}) if isinstance(proj, dict) else {}
                servers = (mcp_cfg.get("servers") or []) if isinstance(mcp_cfg, dict) else []
                masked = []
                for s in servers:
                    if not isinstance(s, dict):
                        continue
                    s2 = {k: v for k, v in s.items() if k != "headers"}
                    if s.get("headers"):
                        s2["has_headers"] = True
                    masked.append(s2)
                if masked:
                    proj.setdefault("mcp", {})["servers"] = masked
            except Exception:
                pass
            if owner:
                proj["owner"] = owner
            # Frontend hint: who can delete this agent under current policy
            proj["can_delete"] = bool(is_admin or a.get("owner_id") == user.get("id"))
            enriched.append(proj)
        except Exception:
            enriched.append(a)
    # Apply storage cleanup if we removed deprecated fields
    try:
        if changed_any:
            _save_all(data)
    except Exception:
        pass

    # include single global active id for backwards compatibility
    active = _load_active_global()
    return jsonify({"agents": enriched, "active_agent_id": active.get("id") if isinstance(active, dict) else None})


@app.post("/agents")
@require_auth()
def create_agent():
    user = g.current_user
    if user.get("role") != "admin" and not _internal_key_matches():
        return jsonify({"error": "forbidden"}), 403
    payload = request.json or {}
    data = _load_all()
    agent_id = payload.get("id") or uuid.uuid4().hex
    payload["id"] = agent_id
    payload["owner_id"] = user.get("id")
    data["agents"].append(payload)
    _save_all(data)
    # Generate greeting audio asynchronously best-effort using thread
    _synthesize_greeting_async(payload, agent_id)
    return jsonify(payload), 201


@app.post("/internal/agents")
@require_auth()
@require_internal()
def internal_create_agent():
    """Backend-only: create an agent owned by the authenticated user.

    Protected by X-Internal-Key to prevent UI-triggered creation for non-admins.
    """
    user = g.current_user
    payload = request.json or {}
    data = _load_all()
    agent_id = payload.get("id") or uuid.uuid4().hex
    payload["id"] = agent_id
    payload["owner_id"] = user.get("id")
    data.setdefault("agents", []).append(payload)
    _save_all(data)
    _synthesize_greeting_async(payload, agent_id)
    return jsonify(payload), 201


@app.put("/agents/<agent_id>")
@require_auth()
def update_agent(agent_id):
    user = g.current_user
    is_admin = user.get("role") == "admin"
    if not is_admin and not _internal_key_matches():
        return jsonify({"error": "forbidden"}), 403
    payload = request.json or {}
    data = _load_all()
    agents = data.get("agents", [])
    for i, a in enumerate(agents):
        if a.get("id") == agent_id:
            # Non-admin internal callers may only edit their own agents
            if not is_admin and a.get("owner_id") != user.get("id"):
                return jsonify({"error": "forbidden"}), 403
            updated = {**a, **payload, "id": agent_id, "owner_id": a.get("owner_id")}
            # Preserve MCP headers when UI sends masked servers without headers
            try:
                if isinstance(payload.get("mcp"), dict) and isinstance((payload.get("mcp") or {}).get("servers"), list):
                    existing_servers = ((a.get("mcp") or {}).get("servers") or []) if isinstance(a.get("mcp"), dict) else []
                    new_servers = ((updated.get("mcp") or {}).get("servers") or []) if isinstance(updated.get("mcp"), dict) else []
                    def _match(prev, cur):
                        try:
                            pu = str((prev or {}).get("url") or "").strip()
                            cu = str((cur or {}).get("url") or "").strip()
                            if pu and cu and pu == cu:
                                return True
                            pn = str((prev or {}).get("name") or "").strip()
                            cn = str((cur or {}).get("name") or "").strip()
                            return bool(pn and cn and pn == cn)
                        except Exception:
                            return False
                    for idx, s in enumerate(new_servers):
                        if not isinstance(s, dict) or ("headers" in s and isinstance(s.get("headers"), dict)):
                            continue
                        # Only attempt copy when headers are missing
                        for prev in existing_servers:
                            if _match(prev, s) and isinstance(prev, dict) and isinstance(prev.get("headers"), dict):
                                s = {k: v for k, v in s.items() if k != "has_headers"}
                                s["headers"] = prev.get("headers")
                                new_servers[idx] = s
                                break
                    updated.setdefault("mcp", {})["servers"] = new_servers
            except Exception:
                pass
            agents[i] = updated
            _save_all(data)
            # If greeting text or voice/provider changed, regenerate greeting audio and delete the old one
            try:
                changed_greeting = ((a.get("greeting", {}) or {}).get("text") != (updated.get("greeting", {}) or {}).get("text"))
                changed_voice = ((a.get("tts", {}) or {}).get("reference_id") != (updated.get("tts", {}) or {}).get("reference_id"))
                old_rt = (a.get("realtime", {}) or {})
                new_rt = (updated.get("realtime", {}) or {})
                changed_provider = str(old_rt.get("tts_provider", "openai")).lower() != str(new_rt.get("tts_provider", "openai")).lower() or bool(old_rt.get("enabled")) != bool(new_rt.get("enabled")) or (old_rt.get("voice") != new_rt.get("voice"))
                missing_asset = True
                try:
                    cur_asset = (updated.get("assets", {}) or {}).get("greeting_audio")
                    if cur_asset:
                        missing_asset = not (Path(__file__).parent / cur_asset).exists()
                except Exception:
                    missing_asset = True
                if changed_greeting or changed_voice or changed_provider or missing_asset:
                    tts_cfg = updated.get("tts", {}) if isinstance(updated.get("tts"), dict) else {}
                    if True:
                        # Delete old greeting file(s) if present
                        try:
                            # remove primary and preview for this agent id
                            base = agent_id
                            gdir = Path(__file__).parent / "greetings"
                            for p in gdir.glob(f"{base}.*"):
                                try:
                                    p.unlink(missing_ok=True)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        # Kick off async synthesis and optimistically set path
                        _synthesize_greeting_async(updated, agent_id)
                        # Choose optimistic extension based on provider
                        rt = (updated.get("realtime", {}) or {}) if isinstance(updated.get("realtime"), dict) else {}
                        if bool(rt.get("enabled")) and str(rt.get("tts_provider", "openai")).lower() == "openai":
                            ext = "wav"
                        else:
                            ext = (tts_cfg.get("response_format") or os.getenv("FISH_AUDIO_RESPONSE_FORMAT", "opus")).lower()
                            if ext not in {"wav","mp3","aac","flac","opus","pcm"}:
                                ext = "opus"
                        updated.setdefault("assets", {})["greeting_audio"] = f"greetings/{agent_id}.{ext}"
                        data["agents"][i] = updated
                        _save_all(data)
            except Exception:
                pass
            return jsonify(updated)
    return jsonify({"error": "not_found"}), 404


@app.get("/livekit/room_token/<room>")
@require_auth()
def issue_room_token(room: str):
    """Issue a viewer token for the specified LiveKit room.
    The token is short-lived and intended for debugging/monitoring.
    """
    try:
        # Disallow reviving ended rooms unless explicitly requested
        try:
            revive = str(request.args.get("revive") or "0").strip() in {"1","true","yes"}
            store = _load_livekit_store()
            ended = False
            # Find call by room name
            for v in (store.get("calls") or {}).values():
                try:
                    rname = v.get("room_name") or (v.get("room") or {}).get("name")
                    if str(rname) == str(room):
                        if v.get("ended_at"):
                            ended = True
                        break
                except Exception:
                    continue
            if ended and not revive:
                return jsonify({"error": "room_ended", "hint": "Pass ?revive=1 to force a token for an ended room."}), 410
        except Exception:
            pass
        # Prefer configured LiveKit settings
        data = _load_all()
        d = _load_defaults()
        lk_cfg = (data.get("defaults", {}) or {}).get("livekit", {})
        lk_def = d.get("livekit", {})
        lk_url = str(lk_cfg.get("url") or lk_def.get("url") or os.getenv("LIVEKIT_URL", ""))
        lk_key = str(lk_cfg.get("api_key") or lk_def.get("api_key") or os.getenv("LIVEKIT_API_KEY", ""))
        lk_secret = str(lk_cfg.get("api_secret") or lk_def.get("api_secret") or os.getenv("LIVEKIT_API_SECRET", ""))
        if not (lk_url and lk_key and lk_secret):
            return jsonify({"error":"missing_livekit_env", "hint":"Set LIVEKIT_URL/KEY/SECRET in server env or in defaults.livekit."}), 500

        identity = f"viewer-{_uuid.uuid4().hex[:6]}"
        token = lk_api.AccessToken(lk_key, lk_secret)
        token.with_identity(identity)
        token.with_name(identity)
        grants = lk_api.VideoGrants(
            room=room,
            room_join=True,
            can_publish=False,
            can_subscribe=True,
        )
        token.with_grants(grants)
        jwt = token.to_jwt()
        return jsonify({"server_url": lk_url, "room": room, "token": jwt, "identity": identity})
    except Exception as e:
        return jsonify({"error": f"token_error: {e}"}), 500

@app.delete("/agents/<agent_id>")
@require_auth()
def delete_agent(agent_id):
    data = _load_all()
    user = g.current_user
    is_admin = user.get("role") == "admin"
    agents = data.get("agents", [])
    target = None
    for a in agents:
        if a.get("id") == agent_id:
            target = a
            break
    if not target:
        return jsonify({"error": "not_found"}), 404
    if (not is_admin) and (target.get("owner_id") != user.get("id")):
        return jsonify({"error": "forbidden"}), 403
    before = len(agents)
    data["agents"] = [a for a in agents if a.get("id") != agent_id]
    _save_all(data)
    return ("", 204) if len(data["agents"]) < before else (jsonify({"error": "not_found"}), 404)


@app.put("/active/<agent_id>")
@require_auth()
def set_active(agent_id):
    data = _load_all()
    user = g.current_user
    for a in data.get("agents", []):
        if a.get("id") == agent_id:
            if user.get("role") != "admin" and a.get("owner_id") != user.get("id"):
                # Allow non-admins to activate agents owned by any admin (read-only sharing)
                owner = _get_user_by_id(a.get("owner_id"))
                if not owner or owner.get("role") != "admin":
                    return jsonify({"error": "forbidden"}), 403
            _set_active_for_user(user.get("id"), a)
            return jsonify({"ok": True, "active_agent_id": agent_id})
    return jsonify({"error": "not_found"}), 404


@app.get("/active")
@require_auth()
def get_active():
    user = g.current_user
    data = _load_active_for_user(user.get("id"))
    return jsonify(data or {})


@app.put("/active")
@require_auth()
def update_active_config():
    payload = request.json or {}
    user = g.current_user
    current = _load_active_for_user(user.get("id"))
    updated = _deep_merge(current if isinstance(current, dict) else {}, payload if isinstance(payload, dict) else {})
    # Preserve MCP headers when UI sends masked servers without headers for active agent
    try:
        if isinstance((payload or {}).get("mcp"), dict) and isinstance(((payload or {}).get("mcp") or {}).get("servers"), list):
            existing_servers = ((current or {}).get("mcp", {}) or {}).get("servers") or []
            new_servers = ((updated or {}).get("mcp", {}) or {}).get("servers") or []
            def _match(prev, cur):
                try:
                    pu = str((prev or {}).get("url") or "").strip(); cu = str((cur or {}).get("url") or "").strip()
                    if pu and cu and pu == cu: return True
                    pn = str((prev or {}).get("name") or "").strip(); cn = str((cur or {}).get("name") or "").strip()
                    return bool(pn and cn and pn == cn)
                except Exception:
                    return False
            for idx, s in enumerate(new_servers):
                if not isinstance(s, dict) or ("headers" in s and isinstance(s.get("headers"), dict)):
                    continue
                for prev in existing_servers:
                    if _match(prev, s) and isinstance(prev, dict) and isinstance(prev.get("headers"), dict):
                        s = {k: v for k, v in s.items() if k != "has_headers"}
                        s["headers"] = prev.get("headers")
                        new_servers[idx] = s
                        break
            updated.setdefault("mcp", {})["servers"] = new_servers
    except Exception:
        pass
    # If greeting text or voice changed, regenerate active greeting audio too
    try:
        old_g = ((current or {}).get("greeting", {}) or {}).get("text") if isinstance(current, dict) else None
        new_g = ((updated or {}).get("greeting", {}) or {}).get("text") if isinstance(updated, dict) else None
        old_v = ((current or {}).get("tts", {}) or {}).get("reference_id") if isinstance(current, dict) else None
        new_v = ((updated or {}).get("tts", {}) or {}).get("reference_id") if isinstance(updated, dict) else None
        old_rt = (current.get("realtime", {}) if isinstance(current, dict) else {}) or {}
        new_rt = (updated.get("realtime", {}) if isinstance(updated, dict) else {}) or {}
        changed_provider = str(old_rt.get("tts_provider", "openai")).lower() != str(new_rt.get("tts_provider", "openai")).lower() or bool(old_rt.get("enabled")) != bool(new_rt.get("enabled")) or (old_rt.get("voice") != new_rt.get("voice"))
        missing_asset = True
        try:
            cur_asset = ((updated or {}).get("assets", {}) or {}).get("greeting_audio") if isinstance(updated, dict) else None
            if cur_asset:
                missing_asset = not (Path(__file__).parent / cur_asset).exists()
        except Exception:
            missing_asset = True
        if old_g != new_g or old_v != new_v or changed_provider or missing_asset:
            # delete old file(s) if present
            try:
                base = (updated.get("id") or "active")
                gdir = Path(__file__).parent / "greetings"
                for p in gdir.glob(f"{base}.*"):
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass
            except Exception:
                pass
            active_id = updated.get("id") or "active"
            _synthesize_greeting_async(updated, active_id)
            # Optimistic extension by provider
            tts_cfg = updated.get("tts", {}) if isinstance(updated.get("tts"), dict) else {}
            if bool(new_rt.get("enabled")) and str(new_rt.get("tts_provider", "openai")).lower() == "openai":
                ext = "wav"
            else:
                ext = (tts_cfg.get("response_format") or os.getenv("FISH_AUDIO_RESPONSE_FORMAT", "opus")).lower()
                if ext not in {"wav","mp3","aac","flac","opus","pcm"}:
                    ext = "opus"
            updated.setdefault("assets", {})["greeting_audio"] = f"greetings/{active_id}.{ext}"
    except Exception:
        pass
    _set_active_for_user(user.get("id"), updated)
    return jsonify(updated)


# ------------------ AUTH & USERS ------------------

def _is_first_user() -> bool:
    return len(_load_users().get("users", [])) == 0


@app.post("/auth/register")
def register():
    payload = request.json or {}
    username = (payload.get("username") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    if not username or not email or not password:
        return jsonify({"error": "missing_fields"}), 400
    users = _load_users()
    for u in users.get("users", []):
        if u.get("email") == email:
            return jsonify({"error": "email_exists"}), 409
    user = {
        "id": uuid.uuid4().hex,
        "username": username,
        "email": email,
        "password_hash": generate_password_hash(password),
        "role": "admin" if _is_first_user() else "user",
        "created_at": int(time.time()),
    }
    users.setdefault("users", []).append(user)
    _save_users(users)
    token = _issue_token(user["id"])
    public = {k: v for k, v in user.items() if k != "password_hash"}
    return jsonify({"token": token, "user": public})


@app.post("/auth/login")
def login():
    payload = request.json or {}
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    users = _load_users().get("users", [])
    for u in users:
        if u.get("email") == email and check_password_hash(u.get("password_hash"), password):
            token = _issue_token(u.get("id"))
            public = {k: v for k, v in u.items() if k != "password_hash"}
            return jsonify({"token": token, "user": public})
    return jsonify({"error": "invalid_credentials"}), 401


@app.get("/auth/me")
def me():
    auth = request.headers.get("Authorization", "")
    token = auth.split(" ")[-1] if auth.startswith("Bearer ") else auth
    user = _get_user_by_token(token)
    if not user:
        return jsonify({}), 200
    public = {k: v for k, v in user.items() if k != "password_hash"}
    return jsonify({"user": public})


@app.put("/auth/me")
@require_auth()
def update_me():
    payload = request.json or {}
    users = _load_users()
    updated = None
    for i, u in enumerate(users.get("users", [])):
        if u.get("id") == g.current_user.get("id"):
            if payload.get("username"):
                u["username"] = str(payload["username"]).strip()
            if payload.get("password"):
                u["password_hash"] = generate_password_hash(str(payload["password"]))
            users["users"][i] = u
            updated = {k: v for k, v in u.items() if k != "password_hash"}
            break
    if updated is None:
        return jsonify({"error": "not_found"}), 404
    _save_users(users)
    return jsonify({"user": updated})


@app.get("/defaults")
@require_auth(admin=True)
def get_defaults():
    return jsonify(_load_defaults())


@app.put("/defaults")
@require_auth(admin=True)
def update_defaults():
    payload = request.json or {}
    cur = _load_defaults()
    # Merge shallowly per section (no VAD/turn settings anywhere)
    for section in ("llm", "stt", "tts", "realtime", "livekit", "openai", "env"):
        if isinstance(payload.get(section), dict):
            cur[section] = {**(cur.get(section) or {}), **payload.get(section)}
    _save_defaults(cur)
    return jsonify(cur)

"""Removed .env overlay endpoints by request. Runtime should source from process env or defaults sections only."""


@app.post("/auth/forgot")
def forgot_password():
    payload = request.json or {}
    email = (payload.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "missing_email"}), 400
    users = _load_users().get("users", [])
    for u in users:
        if u.get("email") == email:
            token = _create_reset_token(u.get("id"))
            # In production: send email here. For now, return token in response.
            return jsonify({"ok": True, "reset_token": token})
    return jsonify({"ok": True})  # do not reveal if email exists


@app.post("/auth/reset")
def reset_password():
    payload = request.json or {}
    token = payload.get("token")
    new_password = payload.get("password")
    if not token or not new_password:
        return jsonify({"error": "missing"}), 400
    user_id = _consume_reset_token(token)
    if not user_id:
        return jsonify({"error": "invalid_or_expired"}), 400
    users = _load_users()
    for i, u in enumerate(users.get("users", [])):
        if u.get("id") == user_id:
            u["password_hash"] = generate_password_hash(str(new_password))
            users["users"][i] = u
            _save_users(users)
            return jsonify({"ok": True})
    return jsonify({"error": "not_found"}), 404


@app.get("/users")
@require_auth(admin=True)
def list_users():
    users = _load_users().get("users", [])
    # Auto-assign any agents missing owner_id to the first admin user to maintain consistency
    data = _load_all()
    agents = data.get("agents", [])
    missing = [a for a in agents if not a.get("owner_id")]
    if missing:
        admin_user = next((u for u in users if u.get("role") == "admin"), None)
        if admin_user:
            for a in agents:
                if not a.get("owner_id"):
                    a["owner_id"] = admin_user.get("id")
            data["agents"] = agents
            _save_all(data)
    rows = []
    for u in users:
        public = {k: v for k, v in u.items() if k != "password_hash"}
        u_agents = [a for a in agents if a.get("owner_id") == u.get("id")]
        public["agents"] = u_agents
        rows.append(public)
    return jsonify({"users": rows})


# ------------------ ADMIN SEED DEMO USER & MIGRATIONS ------------------

def _seed_demo_user(email: str | None = None, password: str | None = None) -> dict | None:
    users = _load_users()
    for u in users.get("users", []):
        if u.get("username").lower() == "zainlee_demo" or u.get("username").lower() == "zainlee demo":
            return None  # already exists
    pwd = password or secrets.token_hex(4)
    demo = {
        "id": uuid.uuid4().hex,
        "username": "Zainlee_Demo",
        "email": (email or "zainlee_demo@local"),
        "password_hash": generate_password_hash(pwd),
        "role": "user",
        "created_at": int(time.time()),
    }
    users.setdefault("users", []).append(demo)
    _save_users(users)
    return {"user": {k: v for k, v in demo.items() if k != "password_hash"}, "password": pwd}


def _migrate_agents_assign(owner_id: str) -> int:
    data = _load_all()
    changed = 0
    for a in data.get("agents", []):
        if not a.get("owner_id"):
            a["owner_id"] = owner_id
            changed += 1
    _save_all(data)
    return changed


@app.post("/admin/seed_demo")
@require_auth(admin=True)
def admin_seed_demo():
    payload = request.json or {}
    created = _seed_demo_user(payload.get("email"), payload.get("password"))
    if created is None:
        # Already exists; find id
        for u in _load_users().get("users", []):
            if u.get("username").lower() in {"zainlee_demo", "zainlee demo"}:
                demo_id = u.get("id")
                migrated = _migrate_agents_assign(demo_id)
                return jsonify({"status": "exists", "migrated_agents": migrated})
        return jsonify({"status": "exists", "migrated_agents": 0})
    migrated = _migrate_agents_assign(created["user"]["id"])
    return jsonify({"status": "created", **created, "migrated_agents": migrated})


# Legacy usage endpoints removed


"""Legacy subscription endpoints removed"""


# Simple listing endpoint for uploaded files (optional)
@app.get("/uploads/<path:filename>")
def get_uploaded_file(filename):
    base_dir = Path(__file__).parent
    # allow serving only within the two upload directories
    if filename.startswith("background_ambience/"):
        return send_from_directory(str(base_dir / "background_ambience"), filename.split("/", 1)[1])
    # thinking_sounds removed
    if filename.startswith("greetings/"):
        return send_from_directory(str(base_dir / "greetings"), filename.split("/", 1)[1])
    return jsonify({"error": "not_allowed"}), 403


@app.get("/transcripts")
def list_transcripts():
    base_dir = Path(__file__).parent / "transcripts"
    base_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for p in sorted(base_dir.glob("call_*.json"), reverse=True):
        files.append({
            "filename": p.name,
            "path": f"transcripts/{p.name}",
            "modified": p.stat().st_mtime,
        })
    return jsonify({"files": files})


@app.get("/transcripts/<path:filename>")
def get_transcript(filename):
    base_dir = Path(__file__).parent / "transcripts"
    safe = secure_filename(filename)
    target = base_dir / safe
    if not target.exists():
        return jsonify({"error": "not_found"}), 404
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/transcripts/by_room/<room_name>")
def get_transcript_by_room(room_name: str):
    """Return the latest transcript whose 'room' equals room_name."""
    base_dir = Path(__file__).parent / "transcripts"
    base_dir.mkdir(parents=True, exist_ok=True)
    try:
        # Iterate newest first
        for p in sorted(base_dir.glob("call_*.json"), reverse=True):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if str(data.get("room") or "") == str(room_name):
                    return jsonify(data)
            except Exception:
                continue
        return jsonify({}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/transcripts/save")
def save_transcript():
    """Save real-time transcript from web UI."""
    try:
        payload = request.json or {}
        room = payload.get("room")
        agent_id = payload.get("agent_id")
        entries = payload.get("entries", [])
        
        if not room or not entries:
            return jsonify({"error": "missing_data"}), 400
        
        base_dir = Path(__file__).parent / "transcripts"
        base_dir.mkdir(parents=True, exist_ok=True)
        
        # Create transcript file
        timestamp = int(time.time() * 1000)
        filename = f"call_{room}_{timestamp}.json"
        filepath = base_dir / filename
        
        transcript_data = {
            "room": room,
            "agent_id": agent_id,
            "timestamp": timestamp,
            "entries": entries,
            "created_at": time.time()
        }
        
        filepath.write_text(json.dumps(transcript_data, indent=2), encoding="utf-8")
        
        return jsonify({"ok": True, "filename": filename})
    except Exception as e:
        print(f"Error saving transcript: {e}", file=sys.stderr)
        return jsonify({"error": str(e)}), 500


@app.get("/analytics/summary")
def analytics_summary():
    """Return basic analytics derived from saved transcripts.
    Provides: total_calls, average_duration_seconds.
    """
    base_dir = Path(__file__).parent / "transcripts"
    base_dir.mkdir(parents=True, exist_ok=True)
    total_calls = 0
    total_duration = 0.0
    for p in base_dir.glob("call_*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            start = data.get("started_at")
            end = data.get("ended_at")
            if start and end:
                d = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
                total_duration += max(0.0, d)
            total_calls += 1
        except Exception:
            continue
    avg = (total_duration / total_calls) if total_calls > 0 else 0.0
    return jsonify({
        "total_calls": total_calls,
        "average_duration_seconds": avg,
    })


@app.get("/analytics/calls_over_time")
def analytics_calls_over_time():
    """Return daily call counts derived from transcript filenames."""
    base_dir = Path(__file__).parent / "transcripts"
    base_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for p in base_dir.glob("call_*.json"):
        try:
            # Filename format: call_YYYYMMDD_HHMMSS.json
            name = p.stem  # call_YYYYMMDD_HHMMSS
            date_part = name.split("_")[1]
            date_fmt = f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]}"
            counts[date_fmt] = counts.get(date_fmt, 0) + 1
        except Exception:
            continue
    points = sorted(({"date": k, "count": v} for k, v in counts.items()), key=lambda x: x["date"])
    return jsonify({"points": points})


# ------------------ LIVEKIT WEBHOOK ENDPOINT ------------------

def _lk_get_or_create_call(store: dict, sip_call_id: str) -> Dict[str, Any]:
    calls = store.setdefault("calls", {})
    call = calls.get(sip_call_id) or {
        "id": sip_call_id,
        "events": [],
        "room": {},
        "timeline": [],
        "created_at": int(time.time()),
    }
    calls[sip_call_id] = call
    return call


@app.post("/webhooks/livekit")
def livekit_webhook_receiver():
    """Minimal receiver that stores LiveKit Cloud webhook events.
    It does not verify signatures in this demo setup. For production, verify
    the Authorization header per LiveKit docs.
    """
    raw = request.get_data(cache=False, as_text=True) or "{}"
    auth_header = request.headers.get("Authorization")
    # Validate signature if configured
    receiver = None
    if LKWebhookReceiver and os.getenv("LIVEKIT_WEBHOOK_KEY") and os.getenv("LIVEKIT_WEBHOOK_SECRET"):
        try:
            receiver = LKWebhookReceiver(os.getenv("LIVEKIT_WEBHOOK_KEY"), os.getenv("LIVEKIT_WEBHOOK_SECRET"))
            receiver.receive(raw, auth_header)  # raises on invalid
        except Exception:
            return jsonify({"error": "invalid_signature"}), 401
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {}
    event = str(payload.get("event") or payload.get("type") or "").lower()
    store = _load_livekit_store()

    # Identify call/session keys. Many LiveKit events include sip_call_id, while others only include room
    room_name = (payload.get("room") or {}).get("name")
    sip_call_id = (
        payload.get("sip", {}).get("call", {}).get("id")
        or payload.get("sip_call", {}).get("id")
        or payload.get("sip_call_id")
    )
    # Maintain explicit room<->call mappings to unify events for the same session
    room_to_call = store.setdefault("room_to_call", {})
    call_to_room = store.setdefault("call_to_room", {})
    calls = store.setdefault("calls", {})

    # Determine canonical call id
    canonical_call_id = None
    if sip_call_id:
        canonical_call_id = str(sip_call_id)
    elif room_name and room_to_call.get(str(room_name)):
        canonical_call_id = str(room_to_call.get(str(room_name)))
    else:
        # Fallback for non-SIP events before SIP metadata arrives
        canonical_call_id = str(room_name or payload.get("id") or "unknown")

    # If SIP metadata arrives after early room-only events, merge any legacy room-keyed record
    try:
        if sip_call_id and room_name:
            legacy_key = str(room_name)
            target_key = str(sip_call_id)
            if legacy_key != target_key and legacy_key in calls:
                legacy = calls.get(legacy_key) or {}
                target = calls.get(target_key) or {
                    "id": target_key,
                    "events": [],
                    "room": {},
                    "timeline": [],
                    "created_at": int(time.time()),
                }
                # Merge arrays and maps conservatively
                try:
                    target.setdefault("events", []).extend(legacy.get("events", []))
                except Exception:
                    pass
                try:
                    target.setdefault("timeline", []).extend(legacy.get("timeline", []))
                except Exception:
                    pass
                try:
                    if isinstance(legacy.get("room"), dict):
                        target["room"] = {**target.get("room", {}), **legacy.get("room", {})}
                except Exception:
                    pass
                try:
                    lp = legacy.get("participants") or {}
                    if isinstance(lp, dict):
                        tp = target.setdefault("participants", {})
                        for k, v in lp.items():
                            tp[str(k)] = v
                except Exception:
                    pass
                try:
                    # Preserve earliest creation time
                    target["created_at"] = min(int(target.get("created_at", int(time.time()))), int(legacy.get("created_at", int(time.time()))))
                except Exception:
                    pass
                calls[target_key] = target
                # Drop legacy room-keyed record
                try:
                    del calls[legacy_key]
                except Exception:
                    pass
                # Update canonical mappings
                room_to_call[str(room_name)] = target_key
                call_to_room[target_key] = str(room_name)
                canonical_call_id = target_key
            else:
                # Establish/refresh mapping for future room-only events
                room_to_call[str(room_name)] = str(sip_call_id)
                call_to_room[str(sip_call_id)] = str(room_name)
    except Exception:
        pass

    call = _lk_get_or_create_call(store, str(canonical_call_id))
    # Classify event type for safer updating of ended calls
    participant_obj = payload.get("participant") or {}
    part_kind = str((participant_obj.get("kind") or "")).upper()
    identity_val = str(participant_obj.get("identity") or "")
    is_sip_related = bool(sip_call_id) or part_kind == "SIP" or event.startswith("sip_") or bool(payload.get("sip"))
    # Don't bump last_* on non-SIP viewer/debug events for rooms already marked ended
    if not (call.get("ended_at") and not is_sip_related):
        call["last_event"] = event
        call["last_received_at"] = int(time.time())
    # Basic overlays
    if isinstance(payload.get("room"), dict):
        call["room"] = {**call.get("room", {}), **payload.get("room", {})}
    if isinstance(payload.get("participant"), dict):
        parts = call.setdefault("participants", {})
        pid = payload["participant"].get("identity") or payload["participant"].get("sid") or "unknown"
        parts[str(pid)] = payload["participant"]
    # Append event
    call.setdefault("events", []).append(payload)
    # Simple timeline entry
    call.setdefault("timeline", []).append({"t": int(time.time()), "event": event})
    # Persist a few common fields for list rendering
    if payload.get("room") and isinstance(payload["room"], dict):
        call["room_name"] = payload["room"].get("name") or call.get("room_name")
    # Keep event timestamps if provided
    if isinstance(payload.get("created_at"), (int, float)):
        call.setdefault("event_times", []).append({"event": event, "ts": int(payload.get("created_at"))})

    # Attach agent runtime metadata once per call if available via env (set by agent worker)
    try:
        if not call.get("agent_runtime"):
            call["agent_runtime"] = {
                "llm": os.getenv("AGENT_RUNTIME_LLM", ""),
                "tts": os.getenv("AGENT_RUNTIME_TTS", ""),
                "stt": os.getenv("AGENT_RUNTIME_STT", ""),
            }
    except Exception:
        pass

    _save_livekit_store(store)
    # Stop worker promptly on room/call end events
    try:
        room_name_for_close = (payload.get("room") or {}).get("name") or call.get("room", {}).get("name") or call.get("room_name")
        end_like = {"room_finished","room_ended","room_deleted","ingress_ended","sip_call_ended","call_ended"}
        if room_name_for_close and event in end_like:
            proc = _PLAYGROUND_PROCS.pop(room_name_for_close, None)
            if proc is not None:
                try:
                    proc.terminate()
                    # Wait briefly to avoid zombies, then force kill if still alive
                    for _ in range(10):
                        if proc.poll() is not None:
                            break
                        time.sleep(0.2)
                    if proc.poll() is None:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    try:
                        proc.wait(timeout=1)
                    except Exception:
                        pass
                except Exception:
                    pass
            lf = _PLAYGROUND_LOGFILES.pop(room_name_for_close, None)
            if lf is not None:
                try:
                    lf.flush(); lf.close()
                except Exception:
                    pass
            # Clear room<->call mappings to avoid attributing future events to an ended session
            try:
                # Prefer current sip_call_id; otherwise resolve via mapping
                cid = None
                if sip_call_id:
                    cid = str(sip_call_id)
                else:
                    cid = (store.get("room_to_call", {}) or {}).get(str(room_name_for_close))
                try:
                    if cid:
                        (store.get("call_to_room", {}) or {}).pop(str(cid), None)
                except Exception:
                    pass
                try:
                    (store.get("room_to_call", {}) or {}).pop(str(room_name_for_close), None)
                except Exception:
                    pass
                # Mark call as ended for summary views
                try:
                    if cid and isinstance(store.get("calls", {}).get(str(cid)), dict):
                        store["calls"][str(cid)]["ended_at"] = int(time.time())
                except Exception:
                    pass
                _save_livekit_store(store)
            except Exception:
                pass
    except Exception:
        pass
    # Auto-spawn/dispatch agent on first SIP participant/ringing
    try:
        room_name = (payload.get("room") or {}).get("name") or call.get("room", {}).get("name") or call.get("room_name")
        part = payload.get("participant") or {}
        # Extract called number if present in SIP metadata
        sip_meta = {**payload.get("sip", {}).get("call", {}), **{k: v for k, v in (part.get("attributes") or {}).items() if k.startswith("sip.")}}
        called_num = sip_meta.get("phoneNumber") or sip_meta.get("trunkPhoneNumber") or sip_meta.get("to")
        call_status = sip_meta.get("callStatus") or (payload.get("sip", {}).get("call", {}) or {}).get("status")
        should_spawn = False
        if event in {"participant_joined", "ingress_started", "sip_call_started"}:
            if str((part.get("kind") or "").upper()) == "SIP" or event != "participant_joined":
                should_spawn = True
        # 1) Local worker autospawn path (disabled when WEBHOOK_AUTOSPAWN=0)
        if ENABLE_WEBHOOK_AUTOSPAWN and should_spawn and room_name:
            # De-dupe: avoid spawning twice for the same room
            with _SPAWN_LOCK:
                if room_name in _PLAYGROUND_PROCS or room_name in _SPAWN_IN_PROGRESS:
                    return jsonify({"ok": True})
                _SPAWN_IN_PROGRESS.add(room_name)
            # Map number -> agent id (user-scoped routes take precedence)
            routes = _load_routes_union()
            agent_id = None
            if called_num:
                key_e164 = _normalize_e164(called_num)
                key_digits = _digits_only(called_num)
                if key_e164 in routes:
                    agent_id = (routes.get(key_e164) or {}).get("agent_id")
                elif key_digits in routes:
                    agent_id = (routes.get(key_digits) or {}).get("agent_id")
            # Fallback to active/global agent
            if not agent_id:
                try:
                    agent_id = (_load_active_global() or {}).get("id")
                except Exception:
                    agent_id = None
            if agent_id:
                # Spawn worker bound to this room
                try:
                    env = os.environ.copy()
                    env.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", os.getenv("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python"))
                    env.setdefault("PYTHONUNBUFFERED", "1")
                    # Force CPU-only execution to avoid noisy GPU/VAAPI init warnings on servers without drivers
                    env["RUN_AS_WORKER"] = "1"
                    # Join the SIP room by direct token connect
                    env["PLAYGROUND_ROOM_NAME"] = str(room_name)
                    env["PLAYGROUND_FORCE_DIRECT"] = "1"
                    try:
                        env.setdefault("PLAYGROUND_IDENTITY", f"agent-{_uuid.uuid4().hex[:6]}")
                    except Exception:
                        pass
                    env["AGENT_PROFILE_ID"] = str(agent_id)
                    # Ensure worker registers with AGENT_NAME equal to the ASSIGNED agent number (route key), not the caller
                    try:
                        def _normalize_number(raw: str) -> str:
                            s = str(raw or "").strip()
                            if s.startswith("+"):
                                return "+" + re.sub(r"[^0-9]", "", s[1:])
                            return re.sub(r"[^0-9]", "", s)
                        def _digits_only(raw: str) -> str:
                            return re.sub(r"[^0-9]", "", str(raw or ""))

                        assigned_num_key = None
                        called_digits = _digits_only(called_num) if called_num else None
                        # 1) Prefer route that matched this agent AND matches called number (ignoring formatting)
                        for rk, rv in (routes or {}).items():
                            try:
                                if str((rv or {}).get("agent_id")) == str(agent_id):
                                    if called_digits and _digits_only(rk) == called_digits:
                                        assigned_num_key = rk
                                        break
                            except Exception:
                                continue
                        # 2) Otherwise, pick any route number assigned to this agent
                        if not assigned_num_key:
                            for rk, rv in (routes or {}).items():
                                try:
                                    if str((rv or {}).get("agent_id")) == str(agent_id):
                                        assigned_num_key = rk
                                        break
                                except Exception:
                                    continue
                        # 3) Otherwise, try the globally selected number if it maps to this agent
                        if not assigned_num_key:
                            try:
                                sel = (_load_numbers() or {}).get("selected_number")
                                if sel and str(((routes or {}).get(sel) or {}).get("agent_id")) == str(agent_id):
                                    assigned_num_key = sel
                            except Exception:
                                pass
                        # 4) Normalize for AGENT_NAME; fallback to room-derived digits or random id
                        if assigned_num_key:
                            num = _normalize_number(assigned_num_key)
                        else:
                            # Attempt to extract a number from room name as last resort
                            m = re.search(r"([+]?\d{6,})", str(room_name))
                            num = _normalize_number(m.group(1)) if m else f"agent-{_uuid.uuid4().hex[:6]}"
                        env["AGENT_NAME"] = num
                        env.setdefault("DIALED_NUMBER", num)
                        env.setdefault("AGENT_PHONE_NUMBER", num)
                    except Exception:
                        env["AGENT_NAME"] = f"agent-{_uuid.uuid4().hex[:6]}"
                    # Silence GPU/VAAPI and multimedia noise
                    env.setdefault("LIBVA_MESSAGING_LEVEL", "0")
                    env.setdefault("FFMPEG_LOG_LEVEL", "error")
                    env.setdefault("GLOG_minloglevel", "2")
                    env.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
                    env.setdefault("CUDA_VISIBLE_DEVICES", "")
                    # Provide config API base for number routing and defaults
                    try:
                        env.setdefault("CONFIG_API_BASE", os.getenv("CONFIG_API_BASE", f"http://localhost:{os.getenv('CONFIG_API_PORT','5057')}"))
                    except Exception:
                        pass
                    # Inject LiveKit and OpenAI creds from defaults if present
                    try:
                        d = _load_defaults() or {}
                    except Exception:
                        d = {}
                    lk_def = (d or {}).get("livekit", {}) or {}
                    env.setdefault("LIVEKIT_URL", str(lk_def.get("url") or os.getenv("LIVEKIT_URL", "")))
                    env.setdefault("LIVEKIT_API_KEY", str(lk_def.get("api_key") or os.getenv("LIVEKIT_API_KEY", "")))
                    env.setdefault("LIVEKIT_API_SECRET", str(lk_def.get("api_secret") or os.getenv("LIVEKIT_API_SECRET", "")))
                    openai_def = (d or {}).get("openai", {}) or {}
                    if openai_def.get("api_key"):
                        env.setdefault("OPENAI_API_KEY", str(openai_def.get("api_key")))
                    if openai_def.get("base_url"):
                        env.setdefault("OPENAI_BASE_URL", str(openai_def.get("base_url")))
                    # Start process
                    agent_path = str((Path(__file__).parent / "agent.py").resolve())
                    try:
                        logs_dir = Path(__file__).parent / "logs"
                        logs_dir.mkdir(parents=True, exist_ok=True)
                        # Unique logfile: number + timestamp + short id
                        base = (env.get("AGENT_NAME") or str(room_name)).strip()
                        ts = time.strftime("%Y%m%d_%H%M%S")
                        short = _uuid.uuid4().hex[:6]
                        log_fp = open(str(logs_dir / f"play_{base}_{ts}_{short}.log"), "wb")
                    except Exception:
                        log_fp = None
                    if log_fp is not None:
                        proc = subprocess.Popen([sys.executable, agent_path], env=env, stdout=log_fp, stderr=subprocess.STDOUT, bufsize=1, universal_newlines=False, preexec_fn=_preexec_child)
                        _PLAYGROUND_LOGFILES[room_name] = log_fp
                    else:
                        proc = subprocess.Popen([sys.executable, agent_path], env=env, preexec_fn=_preexec_child)
                    _PLAYGROUND_PROCS[room_name] = proc
                except Exception:
                    pass
                finally:
                    with _SPAWN_LOCK:
                        _SPAWN_IN_PROGRESS.discard(room_name)
        # 2) Cloud explicit-dispatch fallback: ensure a Cloud Agent joins the room by agent_name (phone number)
        # Enabled when ENABLE_CLOUD_DISPATCH=1 (defaults to on). Safe even if the SIP rule already has agents configured.
        ENABLE_CLOUD_DISPATCH = (os.getenv("ENABLE_CLOUD_DISPATCH", "1") == "1")
        if (not ENABLE_WEBHOOK_AUTOSPAWN) and ENABLE_CLOUD_DISPATCH and should_spawn and room_name:
            try:
                # Determine the agent_name to dispatch using the same routing resolution as local spawn
                routes = _load_routes_union()
                agent_id = None
                if called_num:
                    key_e164 = _normalize_e164(called_num)
                    key_digits = _digits_only(called_num)
                    if key_e164 in routes:
                        agent_id = (routes.get(key_e164) or {}).get("agent_id")
                    elif key_digits in routes:
                        agent_id = (routes.get(key_digits) or {}).get("agent_id")
                # Resolve the phone number key assigned to this agent
                def _digits_only(raw: str) -> str:
                    return re.sub(r"[^0-9]", "", str(raw or ""))
                def _normalize_number(raw: str) -> str:
                    s = str(raw or "").strip()
                    if s.startswith("+"):
                        return "+" + re.sub(r"[^0-9]", "", s[1:])
                    return re.sub(r"[^0-9]", "", s)
                assigned_num_key = None
                called_digits = _digits_only(called_num) if called_num else None
                if agent_id:
                    for rk, rv in (routes or {}).items():
                        try:
                            if str((rv or {}).get("agent_id")) == str(agent_id):
                                if called_digits and _digits_only(rk) == called_digits:
                                    assigned_num_key = rk; break
                        except Exception:
                            continue
                    if not assigned_num_key:
                        for rk, rv in (routes or {}).items():
                            try:
                                if str((rv or {}).get("agent_id")) == str(agent_id):
                                    assigned_num_key = rk; break
                            except Exception:
                                continue
                if not assigned_num_key and called_num:
                    assigned_num_key = called_num
                agent_name_for_dispatch = _normalize_number(assigned_num_key) if assigned_num_key else None
                # Prepare LiveKit API client (convert ws(s) to http(s))
                if agent_name_for_dispatch:
                    try:
                        d = _load_defaults() or {}
                    except Exception:
                        d = {}
                    lk_def = (d or {}).get("livekit", {}) or {}
                    lk_url = str(lk_def.get("url") or os.getenv("LIVEKIT_URL", ""))
                    lk_key = str(lk_def.get("api_key") or os.getenv("LIVEKIT_API_KEY", ""))
                    lk_secret = str(lk_def.get("api_secret") or os.getenv("LIVEKIT_API_SECRET", ""))
                    if lk_url.startswith("wss://"):
                        lk_url_http = "https://" + lk_url[len("wss://"):]
                    elif lk_url.startswith("ws://"):
                        lk_url_http = "http://" + lk_url[len("ws://"):]
                    else:
                        lk_url_http = lk_url
                    if lk_url_http and lk_key and lk_secret:
                        try:
                            client = lk_api.LiveKitAPI(url=lk_url_http, api_key=lk_key, api_secret=lk_secret)
                            # Avoid duplicate dispatches for this room/agent
                            try:
                                existing = client.agent_dispatch.list_dispatch(room_name)
                                # list_dispatch is async in SDK; allow both sync/await forms defensively
                                if hasattr(existing, "__await__"):
                                    existing = existing.__await__()
                            except Exception:
                                existing = []
                            already = False
                            try:
                                for dpo in (existing or []):
                                    if getattr(dpo, "agent_name", None) == agent_name_for_dispatch:
                                        already = True; break
                            except Exception:
                                pass
                            if not already:
                                try:
                                    req = lk_api.CreateAgentDispatchRequest(agent_name=agent_name_for_dispatch, room=room_name)
                                except Exception:
                                    # Older SDKs use protocol types through api namespace
                                    from livekit.protocol.agent_dispatch import CreateAgentDispatchRequest as _Req
                                    req = _Req(agent_name=agent_name_for_dispatch, room=room_name)
                                fut = client.agent_dispatch.create_dispatch(req)
                                if hasattr(fut, "__await__"):
                                    try:
                                        _ = fut.__await__()
                                    except Exception:
                                        pass
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        pass

    return jsonify({"ok": True})


@app.get("/livekit/calls")
@require_auth()
def list_lk_calls():
    store = _load_livekit_store()
    rows = []
    for v in store.get("calls", {}).values():
        # lightweight projection for list (avoid big payloads)
        rows.append({
            "id": v.get("id"),
            "room": {"name": v.get("room_name") or (v.get("room") or {}).get("name")},
            "last_event": v.get("last_event"),
            "last_received_at": v.get("last_received_at"),
        })
    # basic summary sorting, newest first
    rows.sort(key=lambda x: x.get("last_received_at", 0), reverse=True)
    return jsonify({"calls": rows, "agent_name_hint": os.getenv("AGENT_NAME", "")})


# ------------------ INBOUND NUMBER → AGENT ROUTING ------------------

@app.get("/numbers")
def list_numbers():
    """Return mapping of E.164/extension numbers to agent_ids and trunk labels.
    Shape: { routes: { "0523368120": { agent_id: "...", trunk: "Voice Agent V2 - Local" }, ... } }
    """
    return jsonify(_load_numbers())


@app.put("/numbers")
@require_auth(admin=True)
def upsert_numbers():
    """Replace or merge the number routing table.
    Payload: { routes: { number: { agent_id, trunk } } }
    """
    payload = request.json or {}
    cur = _load_numbers()
    routes = cur.get("routes", {})
    for k, v in (payload.get("routes") or {}).items():
        if not isinstance(v, dict):
            continue
        # enforce one agent per number (overwrite) and uniqueness across numbers is naturally satisfied
        routes[str(k)] = { "agent_id": v.get("agent_id"), "trunk": v.get("trunk") }
    cur["routes"] = routes
    # Enforce uniqueness across numbers: only one number may hold a given agent_id
    seen: set[str] = set()
    for n in list(cur["routes"].keys()):
        rid = (cur["routes"][n] or {}).get("agent_id") or ""
        rid = str(rid)
        if rid:
            if rid in seen:
                cur["routes"][n]["agent_id"] = ""
            else:
                seen.add(rid)
    # Optional UI-driven selection of active/worker number
    selected_number = payload.get("selected_number")
    if isinstance(selected_number, str) and selected_number:
        # Allow setting even if not in current routes yet (may be included in payload routes)
        cur["selected_number"] = selected_number
    _save_numbers(cur)
    return jsonify(cur)


@app.delete("/numbers/<number>")
@require_auth(admin=True)
def delete_number(number: str):
    cur = _load_numbers()
    routes = cur.get("routes", {})
    if number in routes:
        del routes[number]
        cur["routes"] = routes
        # If the deleted number was selected, clear the selection
        try:
            if cur.get("selected_number") == number:
                del cur["selected_number"]
        except Exception:
            pass
        _save_numbers(cur)
        return ("", 204)
    return jsonify({"error": "not_found"}), 404


@app.get("/livekit/calls/<call_id>")
@require_auth()
def get_lk_call(call_id: str):
    store = _load_livekit_store()
    data = store.get("calls", {}).get(call_id)
    if not data:
        return jsonify({"error": "not_found"}), 404
    return jsonify(data)


# ------------------ PBX RECORDINGS (ON-DEMAND PROXY) ------------------

def _recordings_api_cfg() -> dict:
    """Return configuration for the external FreePBX Recordings API.
    Uses env vars:
      - RECORDINGS_API_BASE (e.g., http://154.26.136.196:8080)
      - RECORDINGS_API_KEY  (optional X-API-Key header)
    """
    base = os.getenv("RECORDINGS_API_BASE") or os.getenv("FREEPBX_RECORDINGS_API_BASE")
    key = os.getenv("RECORDINGS_API_KEY") or os.getenv("FREEPBX_RECORDINGS_API_KEY")
    return {"base": (base or ""), "key": (key or "")}


def _extract_numbers_from_call(call: dict) -> set[str]:
    """Heuristically extract phone numbers from a stored LiveKit call record.
    Looks at participant identities and SIP metadata within events.
    """
    nums: set[str] = set()
    try:
        # 1) Participant identities often include SIP numbers like "sip_+97148341451"
        for p in (call.get("participants") or {}).values():
            ident = str((p or {}).get("identity") or (p or {}).get("sid") or "")
            m = re.findall(r"[+]?\d{6,}", ident)
            for n in (m or []):
                nums.add(n)
    except Exception:
        pass
    try:
        # 2) SIP metadata in webhook events
        for ev in (call.get("events") or []):
            sip = (ev.get("sip") or {}).get("call") or {}
            for k in ("from", "to", "phoneNumber", "trunkPhoneNumber", "callerNumber", "calleeNumber"):
                v = sip.get(k)
                if isinstance(v, str) and v.strip():
                    m = re.findall(r"[+]?\d{6,}", v)
                    for n in (m or []):
                        nums.add(n)
    except Exception:
        pass
    # Basic normalization: keep as-is and digits-only variant for robust filename matching
    out: set[str] = set()
    for n in nums:
        s = str(n).strip()
        out.add(s)
        try:
            out.add(re.sub(r"[^0-9]", "", s))
        except Exception:
            pass
    return out


def _score_candidates_vs_call_time(items: list[dict], call_epoch: int | None) -> list[dict]:
    if not items:
        return []
    scored = []
    for it in items:
        d = dict(it)
        try:
            mod = float(d.get("modified_ts"))
        except Exception:
            mod = None
        delta = None
        if call_epoch is not None and mod is not None:
            try:
                delta = abs(float(mod) - float(call_epoch))
            except Exception:
                delta = None
        d["delta_seconds"] = delta
        scored.append(d)
    scored.sort(key=lambda x: (x.get("delta_seconds") if x.get("delta_seconds") is not None else 9e18))
    return scored


# ------------------ PBX RECORDINGS INDEXER (BACKGROUND) ------------------

_REC_INDEX_LOCK = threading.Lock()
_REC_INDEXER_STARTED = False


def _rec_log(event: str, **fields) -> None:
    """Structured logging for recordings features (enabled by RECORDINGS_DEBUG=1)."""
    try:
        enabled = (os.getenv("RECORDINGS_DEBUG", "1") == "1")
    except Exception:
        enabled = True
    if not enabled:
        return
    try:
        record = {"ts": int(time.time()), "event": str(event)}
        for k, v in (fields or {}).items():
            # Ensure values are JSON-serializable
            try:
                json.dumps(v)
                record[k] = v
            except Exception:
                record[k] = str(v)
        line = json.dumps(record, ensure_ascii=False)
    except Exception:
        try:
            line = f"{event} {fields}"
        except Exception:
            line = str(event)
    # stdout (captured by pm2/systemd)
    try:
        print(f"[recordings] {line}", flush=True)
    except Exception:
        pass
    # file
    try:
        logs_dir = Path(__file__).parent / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        with open(str(logs_dir / "recordings.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _recordings_index_path() -> Path:
    return _agents_dir() / "recordings_index.json"


def _load_recordings_index() -> dict:
    """Load the local recordings index JSON, creating it if needed."""
    _ensure_storage()
    p = _recordings_index_path()
    if not p.exists():
        try:
            p.write_text(json.dumps({"items": [], "last_scan_ts": 0, "scan_cursor": None, "total": 0}, indent=2), encoding="utf-8")
        except Exception:
            pass
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"items": [], "last_scan_ts": 0, "scan_cursor": None, "total": 0}


def _save_recordings_index(data: dict) -> None:
    """Persist the local recordings index JSON atomically under a lock."""
    try:
        with _REC_INDEX_LOCK:
            _recordings_index_path().write_text(json.dumps(data or {}, indent=2), encoding="utf-8")
    except Exception:
        pass


def _update_index_with_items(index: dict, items: list[dict]) -> dict:
    """Merge a batch of items into the index keyed by relative_path and sort by modified_ts desc."""
    if not isinstance(index, dict):
        index = {}
    existing = {}
    for it in (index.get("items") or []):
        try:
            rp = str(it.get("relative_path") or "")
        except Exception:
            rp = ""
        if rp:
            existing[rp] = {
                "filename": it.get("filename"),
                "relative_path": rp,
                "size_bytes": it.get("size_bytes"),
                "modified_ts": it.get("modified_ts"),
            }
    for it in (items or []):
        try:
            rp = str(it.get("relative_path") or "")
        except Exception:
            rp = ""
        if not rp:
            continue
        existing[rp] = {
            "filename": it.get("filename"),
            "relative_path": rp,
            "size_bytes": it.get("size_bytes"),
            "modified_ts": it.get("modified_ts"),
        }
    merged = list(existing.values())
    try:
        merged.sort(key=lambda x: float(x.get("modified_ts") or 0.0), reverse=True)
    except Exception:
        pass
    index["items"] = merged
    index["total"] = len(merged)
    return index


def _search_index_for_numbers(numbers: set[str], limit: int = 200) -> list[dict]:
    """Find index items whose filename contains any of the provided number variants."""
    idx = _load_recordings_index()
    items = idx.get("items") or []
    results: list[dict] = []
    seen_paths: set[str] = set()
    variants: set[str] = set()
    for n in (numbers or []):
        s = str(n or "").strip()
        if not s:
            continue
        variants.add(s)
        try:
            digits = re.sub(r"[^0-9]", "", s)
            if digits:
                variants.add(digits)
                variants.add("+" + digits)
        except Exception:
            pass
    for it in items:
        fn = str(it.get("filename") or "")
        rp = str(it.get("relative_path") or "")
        if not rp or rp in seen_paths:
            continue
        for v in variants:
            if v and (v in fn):
                results.append(it)
                seen_paths.add(rp)
                break
        if len(results) >= int(limit or 0):
            break
    return results


def _recordings_indexer_tick(pages_per_cycle: int = 3, page_size: int = 500) -> None:
    """One indexer cycle: refresh head page and deepen scan for older pages."""
    cfg = _recordings_api_cfg()
    base = (cfg.get("base") or "").rstrip("/")
    if not base:
        return
    headers = {"Accept": "application/json"}
    if cfg.get("key"):
        headers["X-API-Key"] = cfg.get("key")
    index = _load_recordings_index()
    now = int(time.time())
    # Always refresh head (newest-first)
    try:
        r = requests.get(f"{base}/recordings", headers=headers, params={"page_size": page_size}, timeout=12)
        if r.ok:
            payload = r.json() or {}
            items = payload.get("items") or []
            index = _update_index_with_items(index, items)
            index["last_scan_ts"] = now
            head_next = payload.get("next_offset")
            if index.get("scan_cursor") is None and head_next is not None:
                index["scan_cursor"] = head_next
            _save_recordings_index(index)
            try:
                _rec_log("index_head_ok", count=len(items), page_size=page_size, next_offset=head_next)
            except Exception:
                pass
        else:
            index["last_error"] = f"head_status_{r.status_code}"
            _save_recordings_index(index)
            _rec_log("index_head_error", status=r.status_code)
    except Exception as e:
        index["last_error"] = f"head_exc_{e}"
        _save_recordings_index(index)
        _rec_log("index_head_exception", error=str(e))
        return
    # Deepen scan for older pages
    cursor = index.get("scan_cursor")
    pages_done = 0
    while pages_done < int(pages_per_cycle or 0):
        if cursor is None:
            break
        try:
            r = requests.get(f"{base}/recordings", headers=headers, params={"offset": cursor, "page_size": page_size}, timeout=20)
            if not r.ok:
                index["last_error"] = f"page_status_{r.status_code}"
                _rec_log("index_page_error", status=r.status_code, cursor=cursor)
                break
            payload = r.json() or {}
            items = payload.get("items") or []
            index = _update_index_with_items(index, items)
            index["last_scan_ts"] = now
            cursor = payload.get("next_offset")
            index["scan_cursor"] = cursor
            pages_done += 1
            _rec_log("index_page_ok", page_count=len(items), next_offset=cursor, pages_done=pages_done)
        except Exception as e:
            index["last_error"] = f"page_exc_{e}"
            _rec_log("index_page_exception", error=str(e), cursor=cursor)
            break
    _save_recordings_index(index)


def _ensure_recordings_indexer() -> None:
    """Start the indexer background thread if configured and not already started."""
    global _REC_INDEXER_STARTED
    if _REC_INDEXER_STARTED:
        return
    base = (_recordings_api_cfg().get("base") or "").strip()
    if not base:
        _rec_log("indexer_skip_no_base")
        return
    interval = int(os.getenv("RECORDINGS_INDEX_POLL_SECONDS", "15"))
    pages_per_cycle = int(os.getenv("RECORDINGS_INDEX_PAGES_PER_CYCLE", "3"))
    page_size = int(os.getenv("RECORDINGS_INDEX_PAGE_SIZE", "500"))
    _rec_log("indexer_started", interval=interval, pages_per_cycle=pages_per_cycle, page_size=page_size)

    def _loop() -> None:
        while True:
            try:
                _recordings_indexer_tick(pages_per_cycle=pages_per_cycle, page_size=page_size)
            except Exception:
                pass
            time.sleep(interval)

    try:
        threading.Thread(target=_loop, daemon=True).start()
        _REC_INDEXER_STARTED = True
    except Exception:
        pass


@app.get("/pbx/recordings/index")
@require_auth()
def recordings_index_summary():
    """Return a lightweight summary of the local recordings index (optionally first N items)."""
    idx = _load_recordings_index()
    try:
        limit = int(request.args.get("limit") or "0")
    except Exception:
        limit = 0
    resp = {
        "total": int(idx.get("total") or len(idx.get("items") or [])),
        "last_scan_ts": idx.get("last_scan_ts"),
        "scan_cursor": idx.get("scan_cursor"),
        "error": idx.get("last_error") or "",
    }
    if limit and isinstance(idx.get("items"), list):
        resp["items"] = (idx.get("items") or [])[:limit]
    return jsonify(resp)

try:
    _ensure_recordings_indexer()
except Exception:
    pass


@app.get("/pbx/recordings/for_call/<call_id>")
@require_auth()
def recordings_for_call(call_id: str):
    """Return candidate PBX recordings for a given call and best-guess selection.
    Does not persist any audio; only queries the external Recordings API.
    """
    cfg = _recordings_api_cfg()
    base = (cfg.get("base") or "").rstrip("/")
    if not base:
        return jsonify({"candidates": [], "selected": None, "error": "recordings_api_unconfigured"}), 200

    store = _load_livekit_store()
    call = (store.get("calls") or {}).get(call_id)
    if not call:
        return jsonify({"error": "not_found"}), 404

    # Call reference time: prefer created_at; fallback to first event time
    call_epoch = None
    try:
        call_epoch = int(call.get("created_at"))
    except Exception:
        call_epoch = None
    if call_epoch is None:
        try:
            ets = [int(e.get("created_at")) for e in (call.get("events") or []) if isinstance(e.get("created_at"), (int, float))]
            if ets:
                call_epoch = min(ets)
        except Exception:
            pass

    _rec_log("for_call_start", call_id=call_id)
    numbers = _extract_numbers_from_call(call)
    try:
        _rec_log("for_call_numbers", call_id=call_id, numbers=sorted(list(numbers)))
    except Exception:
        pass
    seen: dict[str, dict] = {}
    # 1) Consult local index first for fast offline-ish results
    try:
        index_before = len(seen)
        for it in _search_index_for_numbers(numbers, limit=500):
            rp = str(it.get("relative_path") or "")
            if rp and rp not in seen:
                seen[rp] = {
                    "filename": it.get("filename"),
                    "relative_path": rp,
                    "size_bytes": it.get("size_bytes"),
                    "modified_ts": it.get("modified_ts"),
                    "source": "index",
                }
        _rec_log("for_call_index_hits", call_id=call_id, hits=(len(seen) - index_before))
    except Exception:
        pass

    # Index-only selection per requirement (no runtime upstream search here)
    _rec_log("for_call_index_only", call_id=call_id, candidates=len(seen))

    candidates = list(seen.values())
    scored = _score_candidates_vs_call_time(candidates, call_epoch)
    selected = scored[0] if scored else None
    try:
        if selected:
            _rec_log("for_call_selected", call_id=call_id, relative_path=selected.get("relative_path"), filename=selected.get("filename"), delta_seconds=selected.get("delta_seconds"))
        else:
            _rec_log("for_call_no_candidates", call_id=call_id)
    except Exception:
        pass
    return jsonify({"candidates": scored, "selected": selected, "call_time": call_epoch})


@app.get("/pbx/recordings/for_call/<call_id>/file")
@require_auth()
def recordings_stream_for_call(call_id: str):
    """Resolve the best recording for a call and stream it directly.

    This wraps recordings_for_call + proxy_recording_file to provide a
    single call for the frontend. Returns 404 if no candidate is found.
    """
    try:
        data = recordings_for_call(call_id)  # Flask response
        # When called internally, jsonify(...) returns a Response; extract JSON
        if hasattr(data, "get_json"):
            payload = data.get_json() or {}
            status = getattr(data, "status_code", 200)
            if status != 200:
                return data
        else:
            # Already a dict
            payload = data or {}
        sel = (payload or {}).get("selected") or {}
        rp = str(sel.get("relative_path") or "")
        if not rp:
            _rec_log("for_call_stream_not_found", call_id=call_id)
            return jsonify({"error": "recording_not_found"}), 404
        _rec_log("for_call_stream_resolve", call_id=call_id, relative_path=rp)
        # Delegate to proxy endpoint
        return proxy_recording_file(rp)
    except Exception as e:
        _rec_log("for_call_stream_exception", call_id=call_id, error=str(e))
        return jsonify({"error": str(e)}), 500


@app.get("/pbx/recordings/file/<path:relative_path>")
@require_auth()
def proxy_recording_file(relative_path: str):
    """Stream a recording file from the PBX Recordings API through this server."""
    cfg = _recordings_api_cfg()
    base = (cfg.get("base") or "").rstrip("/")
    if not base:
        return jsonify({"error": "recordings_api_unconfigured"}), 503
    # Build upstream URL without double-encoding; requests will handle encoding
    url = f"{base}/recordings/file/{relative_path}"
    headers = {}
    if cfg.get("key"):
        headers["X-API-Key"] = cfg.get("key")
    try:
        _rec_log("file_proxy_request", relative_path=relative_path)
        upstream = requests.get(url, headers=headers, stream=True, timeout=20)
        if not upstream.ok:
            try:
                _rec_log("file_proxy_upstream_error", status=upstream.status_code, relative_path=relative_path)
                return jsonify({"error": "upstream_error", "status": upstream.status_code}), upstream.status_code
            finally:
                upstream.close()
        # Pass through content headers
        resp_headers = {}
        ct = upstream.headers.get("Content-Type")
        if ct:
            resp_headers["Content-Type"] = ct
        cd = upstream.headers.get("Content-Disposition")
        if cd:
            resp_headers["Content-Disposition"] = cd
        _rec_log("file_proxy_ok", relative_path=relative_path, content_type=(ct or ""))
        return Response(upstream.iter_content(chunk_size=8192), headers=resp_headers)
    except Exception as e:
        _rec_log("file_proxy_exception", relative_path=relative_path, error=str(e))
        return jsonify({"error": str(e)}), 500


ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".opus"}


def _is_allowed_audio(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in ALLOWED_AUDIO_EXT


# Background audio endpoints removed


    


    


    


def _duplicate_routes_with_prefix(app_obj, prefix: str):
    """Duplicate all registered routes under the given prefix.
    Executed at import-time so it works under WSGI servers too.
    """
    try:
        if not prefix or prefix == "/":
            return
        rules = list(app_obj.url_map.iter_rules())
        for rule in rules:
            if rule.rule.startswith(prefix):
                continue
            if rule.endpoint == 'static':
                continue
            view = app_obj.view_functions.get(rule.endpoint)
            if view is None:
                continue
            try:
                app_obj.add_url_rule(
                    prefix + rule.rule,
                    endpoint=f"{rule.endpoint}__prefixed",
                    view_func=view,
                    methods=rule.methods,
                    defaults=rule.defaults,
                )
            except Exception:
                pass
    except Exception:
        pass

# Always duplicate at import time as well
_duplicate_routes_with_prefix(app, API_PREFIX)
# ------------------ TWILIO INBOUND VOICE WEBHOOK ------------------

def _twilio_validate(req_body: str, url: str, headers, override_token: str | None = None) -> bool:
    # Optional signature validation. Prefer a per-number override token if provided
    token = override_token or os.getenv("TWILIO_AUTH_TOKEN")
    if not token or TwilioRequestValidator is None:
        return True
    try:
        validator = TwilioRequestValidator(token)
        signature = headers.get("X-Twilio-Signature") or headers.get("x-twilio-signature")
        return bool(validator.validate(url, dict(request.form), signature))
    except Exception:
        return False


@app.post("/twilio/voice")
def twilio_voice_webhook():
    """Minimal Twilio inbound voice handler that bridges to LiveKit SIP trunk.

    Uses number→agent routing from numbers.json to choose an agent. The actual
    media is handled by LiveKit SIP trunk; this endpoint returns TwiML to
    redirect Twilio to the LiveKit SIP domain with the original called number.
    """
    try:
        # Resolve per-number auth token for signature validation (multi-tenant)
        called_raw = (request.form.get("To") or request.values.get("To") or "").strip()
        called = _normalize_e164(called_raw)
        per_number_token = _twilio_auth_token_for_number(called)
        if not _twilio_validate(request.get_data(as_text=True) or "", request.url, request.headers, override_token=per_number_token):
            return jsonify({"error": "invalid_signature"}), 401
        
        from_num = (request.form.get("From") or request.values.get("From") or "").strip()
        # Lookup routing (user-scoped routes take precedence over global)
        routes = _load_routes_union()
        route = routes.get(called) or routes.get(_digits_only(called)) or {}
        trunk_label = str(route.get("trunk") or "")
        # LiveKit SIP domain must be provided via env
        sip_domain = os.getenv("LIVEKIT_SIP_DOMAIN") or os.getenv("LIVEKIT_SIP_URI")
        if VoiceResponse is None or not sip_domain:
            # Fallback: simple <Say> if SIP not configured
            vr = VoiceResponse()
            vr.say("Voice service is not configured.")
            return Response(str(vr), mimetype="text/xml")
        # Build TwiML Dial to SIP
        vr = VoiceResponse()
        dial = vr.dial(caller_id=from_num if from_num else None)
        # Pass called number via user part so LiveKit rules can route
        sip_uri = f"sip:{called}@{sip_domain}"
        # Optionally pass trunk label in SIP headers
        headers = {}
        if trunk_label:
            headers["X-Trunk-Label"] = trunk_label
        dial.sip(sip_uri, headers=headers)
        return Response(str(vr), mimetype="text/xml")
    except Exception as e:
        try:
            vr = VoiceResponse()
            vr.say("An error occurred.")
            return Response(str(vr), mimetype="text/xml")
        except Exception:
            return jsonify({"error": str(e)}), 500


# ------------------ TWILIO MULTI-TENANT API ------------------
def _mask_secret(value: str) -> str:
    try:
        s = str(value or "")
        if len(s) <= 6:
            return "*" * len(s)
        return s[:2] + ("*" * (len(s) - 6)) + s[-4:]
    except Exception:
        return ""


@app.get("/twilio/accounts")
@require_auth()
def twilio_list_accounts():
    user = g.current_user
    rows = []
    for acc in _twilio_get_accounts_for_user(user.get("id")):
        rows.append({
            "id": acc.get("id"),
            "label": acc.get("label") or acc.get("account_sid"),
            "account_sid": acc.get("account_sid"),
            "auth_token_masked": _mask_secret(acc.get("auth_token")),
            "created_at": acc.get("created_at"),
        })
    return jsonify({"accounts": rows})


@app.post("/twilio/accounts")
@require_auth()
def twilio_add_account():
    user = g.current_user
    payload = request.json or {}
    sid = (payload.get("account_sid") or "").strip()
    token = (payload.get("auth_token") or "").strip()
    label = (payload.get("label") or "").strip()
    if not sid or not token:
        return jsonify({"error": "missing_fields"}), 400
    store = _load_twilio_store()
    # Disallow duplicates for the same user+sid
    for acc in store.get("accounts", []):
        if acc.get("user_id") == user.get("id") and acc.get("account_sid") == sid:
            return jsonify({"error": "account_exists"}), 409
    rec = {
        "id": uuid.uuid4().hex,
        "user_id": user.get("id"),
        "account_sid": sid,
        "auth_token": token,
        "label": label,
        "created_at": int(time.time()),
    }
    store.setdefault("accounts", []).append(rec)
    _save_twilio_store(store)
    return jsonify({
        "id": rec["id"],
        "label": rec.get("label") or rec.get("account_sid"),
        "account_sid": rec.get("account_sid"),
        "auth_token_masked": _mask_secret(rec.get("auth_token")),
        "created_at": rec.get("created_at"),
    }), 201


@app.delete("/twilio/accounts/<account_id>")
@require_auth()
def twilio_delete_account(account_id: str):
    user = g.current_user
    store = _load_twilio_store()
    before = len(store.get("accounts", []))
    owned = [a for a in store.get("accounts", []) if a.get("id") == account_id and a.get("user_id") == user.get("id")]
    if not owned:
        return jsonify({"error": "not_found_or_forbidden"}), 404
    store["accounts"] = [a for a in store.get("accounts", []) if not (a.get("id") == account_id and a.get("user_id") == user.get("id"))]
    # Remove bound numbers for this account owned by this user
    nums = store.get("numbers", {}) or {}
    for k in list(nums.keys()):
        v = nums.get(k) or {}
        if v.get("user_id") == user.get("id") and v.get("account_id") == account_id:
            try:
                del nums[k]
            except Exception:
                pass
    store["numbers"] = nums
    _save_twilio_store(store)
    return ("", 204) if len(store.get("accounts", [])) < before else (jsonify({"error": "not_found_or_forbidden"}), 404)


@app.get("/twilio/numbers")
@require_auth()
def twilio_list_numbers():
    user = g.current_user
    store = _load_twilio_store()
    rows = []
    for num, meta in (store.get("numbers") or {}).items():
        try:
            if meta.get("user_id") == user.get("id"):
                rows.append({
                    "number": num,
                    "agent_id": meta.get("agent_id"),
                    "trunk": meta.get("trunk"),
                    "account_id": meta.get("account_id"),
                })
        except Exception:
            continue
    return jsonify({"numbers": rows})


@app.put("/twilio/numbers")
@require_auth()
def twilio_upsert_numbers():
    """Upsert per-user number→agent mappings under a specific Twilio account.
    Payload: { numbers: { "+14155550123": { account_id, agent_id, trunk? }, ... } }
    """
    user = g.current_user
    payload = request.json or {}
    entries = (payload.get("numbers") or {})
    if not isinstance(entries, dict) or not entries:
        return jsonify({"error": "invalid_payload"}), 400
    store = _load_twilio_store()
    # Validate all first
    accounts = {a.get("id"): a for a in store.get("accounts", []) if a.get("user_id") == user.get("id")}
    to_write = {}
    for raw_num, conf in entries.items():
        try:
            number_norm = _normalize_e164(raw_num)
            if not isinstance(conf, dict):
                return jsonify({"error": "invalid_entry", "number": raw_num}), 400
            account_id = (conf.get("account_id") or "").strip()
            agent_id = (conf.get("agent_id") or "").strip()
            trunk = (conf.get("trunk") or "").strip()
            if not account_id or not agent_id:
                return jsonify({"error": "missing_fields", "number": raw_num}), 400
            if account_id not in accounts:
                return jsonify({"error": "account_not_owned", "account_id": account_id, "number": raw_num}), 403
            # Ownership check if number already claimed by a different user
            existing = (store.get("numbers", {}) or {}).get(number_norm)
            if existing and existing.get("user_id") != user.get("id") and user.get("role") != "admin":
                return jsonify({"error": "number_in_use", "number": number_norm}), 409
            to_write[number_norm] = {"agent_id": agent_id, "trunk": trunk, "account_id": account_id}
        except Exception:
            return jsonify({"error": "invalid_entry", "number": raw_num}), 400
    # Apply writes
    nums = store.setdefault("numbers", {})
    for n, v in to_write.items():
        nums[n] = {
            "user_id": user.get("id"),
            "account_id": v.get("account_id"),
            "agent_id": v.get("agent_id"),
            "trunk": v.get("trunk"),
        }
    store["numbers"] = nums
    _save_twilio_store(store)
    return jsonify({"ok": True, "numbers": list(to_write.keys())})


@app.delete("/twilio/numbers/<number>")
@require_auth()
def twilio_delete_number(number: str):
    user = g.current_user
    store = _load_twilio_store()
    n = _normalize_e164(number)
    meta = (store.get("numbers", {}) or {}).get(n)
    if not meta:
        return jsonify({"error": "not_found"}), 404
    if meta.get("user_id") != user.get("id") and user.get("role") != "admin":
        return jsonify({"error": "forbidden"}), 403
    try:
        del store.setdefault("numbers", {})[n]
    except Exception:
        pass
    _save_twilio_store(store)
    return ("", 204)


_duplicate_routes_with_prefix(app, API_PREFIX)

# ------------------ VOICES API ------------------
@app.get("/voices")
@require_auth()
def list_voices():
    return jsonify(_load_voices())


@app.post("/voices")
@require_auth(admin=True)
def add_voice():
    payload = request.json or {}
    name = (payload.get("name") or "").strip()
    vid = (payload.get("id") or "").strip()
    if not name or not vid:
        return jsonify({"error": "missing_fields"}), 400
    data = _load_voices()
    # ensure unique id and name
    if any(v.get("id") == vid for v in data.get("voices", [])):
        return jsonify({"error": "id_exists"}), 409
    if any((v.get("name") or "").lower() == name.lower() for v in data.get("voices", [])):
        return jsonify({"error": "name_exists"}), 409
    data.setdefault("voices", []).append({"name": name, "id": vid})
    _save_voices(data)
    return jsonify({"name": name, "id": vid}), 201


@app.delete("/voices/<voice_id>")
@require_auth(admin=True)
def delete_voice(voice_id):
    data = _load_voices()
    before = len(data.get("voices", []))
    data["voices"] = [v for v in data.get("voices", []) if v.get("id") != voice_id]
    if len(data.get("voices", [])) == before:
        return jsonify({"error": "not_found"}), 404
    _save_voices(data)
    return ("", 204)

# Duplicate again after registering new routes so they are available under API_PREFIX
_duplicate_routes_with_prefix(app, API_PREFIX)


# ------------------ MODELS (OPENAI) ------------------
@app.get("/models/openai")
def list_openai_models():
    """Return available OpenAI LLM and STT models for dropdowns.
    Tries env overrides first; otherwise returns a curated safe list.
    """
    # Optional env overrides (comma-separated)
    llm_env = os.getenv("OPENAI_LLM_MODELS")
    stt_env = os.getenv("OPENAI_STT_MODELS")
    def _parse(env_val):
        return [x.strip() for x in str(env_val).split(",") if x.strip()]
    llm_curated = _parse(llm_env) if llm_env else [
        # Restrict to realtime-capable OpenAI models per requirements
        "gpt-4o-mini-realtime",
        "gpt-4o-realtime",
        "gpt-realtime",
    ]
    stt_curated = _parse(stt_env) if stt_env else [
        "gpt-4o-transcribe",
        "gpt-4o-mini-transcribe",
        "whisper-1",
    ]
    # Merge with user-configured defaults (if any)
    d = _load_defaults()
    llm_conf = [m.get("id") if isinstance(m, dict) else str(m) for m in (d.get("llm_models") or [])]
    stt_conf = [m.get("id") if isinstance(m, dict) else str(m) for m in (d.get("stt_models") or [])]
    def _dedup(seq):
        seen = set(); out = []
        for x in seq:
            if not x: continue
            if x not in seen:
                out.append(x); seen.add(x)
        return out
    llm = _dedup(list(llm_conf) + list(llm_curated))
    stt = _dedup(list(stt_conf) + list(stt_curated))
    # Preserve labels if provided in defaults
    def _labelize(ids, defaults_key):
        label_map = { (m.get("id") if isinstance(m,dict) else str(m)): (m.get("label") if isinstance(m,dict) else None) for m in (d.get(defaults_key) or []) }
        return [{"id": mid, "label": label_map.get(mid) or mid} for mid in ids]
    result = {
        "llm": _labelize(llm, "llm_models"),
        "stt": _labelize(stt, "stt_models"),
    }
    return jsonify(result)

# Duplicate routes with prefix for this section as well
_duplicate_routes_with_prefix(app, API_PREFIX)


@app.post("/models/openai")
@require_auth(admin=True)
def add_openai_model():
    payload = request.json or {}
    kind = (payload.get("kind") or "").lower()
    mid = (payload.get("id") or "").strip()
    label = (payload.get("label") or mid).strip()
    if kind not in {"llm","stt"} or not mid:
        return jsonify({"error":"invalid"}), 400
    d = _load_defaults()
    key = "llm_models" if kind=="llm" else "stt_models"
    arr = d.get(key) or []
    # Avoid dupes
    if not any((isinstance(x,dict) and x.get("id")==mid) or (not isinstance(x,dict) and str(x)==mid) for x in arr):
        arr.append({"id": mid, "label": label})
    d[key] = arr
    _save_defaults(d)
    return jsonify(d)


@app.delete("/models/openai/<kind>/<model_id>")
@require_auth(admin=True)
def delete_openai_model(kind: str, model_id: str):
    kind = (kind or "").lower()
    if kind not in {"llm","stt"}:
        return jsonify({"error":"invalid"}), 400
    d = _load_defaults()
    key = "llm_models" if kind=="llm" else "stt_models"
    arr = d.get(key) or []
    arr = [x for x in arr if (isinstance(x,dict) and x.get("id")!=model_id) and (not (not isinstance(x,dict) and str(x)==model_id))]
    d[key] = arr
    _save_defaults(d)
    return jsonify(d)

_duplicate_routes_with_prefix(app, API_PREFIX)


# ------------------ SIMPLE BROWSER TEST AGENT TURN ------------------
@app.post("/testagent/turn")
@require_auth()
def test_agent_turn():
    """Lightweight chat turn for browser testing (no LiveKit).

    Payload: {
      agent_id?: string,
      messages: [ { role: 'user'|'assistant'|'system', content: string } ]
    }

    Returns: { text: string }
    """
    payload = request.json or {}
    agent_id = payload.get("agent_id")
    msgs = payload.get("messages") or []
    # Load agent profile (by id or active)
    agents = _load_all().get("agents", [])
    agent = None
    if agent_id:
        for a in agents:
            if a.get("id") == agent_id:
                agent = a
                break
    if agent is None:
        agent = _load_active_global() or {}
    # Compose system instructions
    system_prompt = (agent.get("instructions") if isinstance(agent, dict) else None) or "You are a helpful voice agent. Keep replies concise."

    # Only OpenAI provider supported in this simple test path
    llm = (agent.get("llm") if isinstance(agent, dict) else {}) or {}
    # Optional overrides from payload
    overrides = payload.get("llm") or {}
    model = overrides.get("model") or llm.get("model") or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    base_url = (llm.get("base_url") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com")
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        return jsonify({"error": "missing_openai_key"}), 400

    # Build messages with system at the top
    chat_messages = [{"role": "system", "content": system_prompt}]
    for m in msgs:
        role = (m or {}).get("role") or "user"
        content = (m or {}).get("content") or ""
        chat_messages.append({"role": role, "content": content})

    try:
        resp = requests.post(
            f"{base_url}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {openai_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": chat_messages,
                "temperature": float(overrides.get("temperature", llm.get("temperature", 0.7))) if isinstance(llm, dict) else 0.7,
                "top_p": float(overrides.get("top_p", llm.get("top_p", 0.9))) if isinstance(llm, dict) else 0.9,
                "max_tokens": 300,
            },
            timeout=30,
        )
        data = resp.json()
        if resp.status_code >= 400:
            return jsonify({"error": data.get("error") or data}), resp.status_code
        text = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
        return jsonify({"text": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/testagent/transcribe")
@require_auth()
def test_agent_transcribe():
    """Transcribe uploaded audio using OpenAI Whisper.

    Accepts multipart/form-data with field 'file'. Returns { text }.
    """
    try:
        if 'file' not in request.files:
            return jsonify({"error":"file_missing"}), 400
        f = request.files['file']
        openai_key = os.getenv("OPENAI_API_KEY")
        if not openai_key:
            return jsonify({"error":"missing_openai_key"}), 400
        files = {
            'file': (f.filename or 'audio.webm', f.stream, f.mimetype or 'audio/webm')
        }
        data = { 'model': 'whisper-1' }
        resp = requests.post(
            'https://api.openai.com/v1/audio/transcriptions',
            headers={'Authorization': f'Bearer {openai_key}'},
            files=files,
            data=data,
            timeout=60
        )
        out = resp.json()
        if resp.status_code >= 400:
            return jsonify({"error": out.get('error') or out}), resp.status_code
        text = out.get('text') or ''
        return jsonify({ 'text': text })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ------------------ LIVE PLAYGROUND: spawn agent + browser token ------------------
_PLAYGROUND_PROCS: dict[str, subprocess.Popen] = {}
_PLAYGROUND_LOGFILES: dict[str, any] = {}
# Prevent duplicate spawns for the same room when multiple webhooks arrive in quick succession
_SPAWN_IN_PROGRESS: set[str] = set()
_SPAWN_LOCK = threading.Lock()

# Ensure spawned workers die if parent dies, and start their own process group
def _preexec_child():
    # Keep preexec simple and async-signal-safe: start a new session/group only
    try:
        import os
        os.setsid()
    except Exception:
        pass

# Reaper thread to clean up finished child records and close log files
def _start_reaper_thread():
    def _reap():
        while True:
            try:
                for room, proc in list(_PLAYGROUND_PROCS.items()):
                    try:
                        if proc.poll() is not None:
                            _PLAYGROUND_PROCS.pop(room, None)
                            lf = _PLAYGROUND_LOGFILES.pop(room, None)
                            if lf:
                                try:
                                    lf.flush(); lf.close()
                                except Exception:
                                    pass
                    except Exception:
                        continue
            except Exception:
                pass
            time.sleep(10)
    try:
        t = threading.Thread(target=_reap, daemon=True)
        t.start()
    except Exception:
        pass

_start_reaper_thread()

# ------------------ LOCAL AGENT SUPERVISOR (PER-NUMBER WORKERS) ------------------
# When enabled, keep a long-lived local supervisor running that registers
# LiveKit Agents workers named by phone number. SIP dispatch rules that target
# those agent names will then dispatch locally without requiring webhook spawn.
_NUMBERS_SUPERVISOR: subprocess.Popen | None = None
_NUMBERS_SUP_LOCK = threading.Lock()

def _start_numbers_supervisor_if_needed() -> None:
    try:
        if os.getenv("ENABLE_LOCAL_SUPERVISOR", "0") != "1":
            return
        global _NUMBERS_SUPERVISOR
        with _NUMBERS_SUP_LOCK:
            if _NUMBERS_SUPERVISOR is not None and _NUMBERS_SUPERVISOR.poll() is None:
                return
            env = os.environ.copy()
            # Provide CONFIG_API_BASE for numbers.json routing lookups
            try:
                env.setdefault("CONFIG_API_BASE", os.getenv("CONFIG_API_BASE", f"http://localhost:{os.getenv('CONFIG_API_PORT','5057')}"))
            except Exception:
                pass
            # Inject LiveKit creds from defaults as a convenience
            try:
                d = _load_defaults() or {}
            except Exception:
                d = {}
            lk_def = (d or {}).get("livekit", {}) or {}
            env.setdefault("LIVEKIT_URL", str(lk_def.get("url") or os.getenv("LIVEKIT_URL", "")))
            env.setdefault("LIVEKIT_API_KEY", str(lk_def.get("api_key") or os.getenv("LIVEKIT_API_KEY", "")))
            env.setdefault("LIVEKIT_API_SECRET", str(lk_def.get("api_secret") or os.getenv("LIVEKIT_API_SECRET", "")))
            # Start agent.py without AGENT_NAME so it runs the per-number supervisor
            agent_path = str((Path(__file__).parent / "agent.py").resolve())
            try:
                logs_dir = Path(__file__).parent / "logs"
                logs_dir.mkdir(parents=True, exist_ok=True)
                log_fp = open(str(logs_dir / f"supervisor.log"), "wb")
            except Exception:
                log_fp = None
            try:
                if log_fp is not None:
                    _NUMBERS_SUPERVISOR = subprocess.Popen([sys.executable, agent_path], env=env, stdout=log_fp, stderr=subprocess.STDOUT, preexec_fn=_preexec_child)
                else:
                    _NUMBERS_SUPERVISOR = subprocess.Popen([sys.executable, agent_path], env=env, preexec_fn=_preexec_child)
            except Exception:
                _NUMBERS_SUPERVISOR = None
    except Exception:
        pass

def _monitor_numbers_supervisor():
    while True:
        try:
            _start_numbers_supervisor_if_needed()
        except Exception:
            pass
        time.sleep(5)

try:
    t = threading.Thread(target=_monitor_numbers_supervisor, daemon=True)
    t.start()
except Exception:
    pass

@app.post("/playground/start")
@require_auth()
def playground_start():
    payload = request.json or {}
    
    # Log full payload (redact sensitive fields)
    debug_payload = {k: v if k not in ['token', 'jwt', 'password'] else '***REDACTED***' for k, v in payload.items()}
    print(f"[playground/start] Full payload received:", file=sys.stderr, flush=True)
    print(json.dumps(debug_payload, indent=2), file=sys.stderr, flush=True)
    
    agent_id = payload.get("agent_id")
    if not agent_id:
        return jsonify({"error":"agent_id_required"}), 400
    
    # Validate and extract tools
    tools = payload.get("tools", [])
    print(f"[playground/start] Received request with {len(tools) if tools else 0} tools in payload", file=sys.stderr, flush=True)
    if tools and not isinstance(tools, list):
        return jsonify({"error": "tools must be an array"}), 400
    
    # Extract global webhook configuration (caller handles routing)
    tools_webhook = payload.get("tools_webhook", {})
    webhook_url = tools_webhook.get("url") if isinstance(tools_webhook, dict) else None
    webhook_headers = tools_webhook.get("headers", {}) if isinstance(tools_webhook, dict) else {}
    webhook_method = tools_webhook.get("method", "POST") if isinstance(tools_webhook, dict) else "POST"
    webhook_timeout = tools_webhook.get("timeout", 10) if isinstance(tools_webhook, dict) else 10
    
    # Normalize tool format: support both OpenAI format and direct format
    normalized_tools = []
    for tool in tools:
        if not isinstance(tool, dict):
            return jsonify({"error": "each tool must be an object"}), 400
        
        # Check if it's OpenAI format with "function" wrapper
        if "type" in tool and tool.get("type") == "function" and "function" in tool:
            # Extract function definition
            func = tool.get("function", {})
            normalized = {
                "name": func.get("name"),
                "description": func.get("description"),
                "parameters": func.get("parameters", {}),
            }
            # Use tool-specific implementation if provided, otherwise use global webhook
            if "implementation" in tool:
                normalized["implementation"] = tool.get("implementation")
            elif webhook_url:
                # Apply global webhook config
                normalized["implementation"] = {
                    "type": "http_callback",
                    "url": webhook_url,
                    "method": webhook_method,
                    "headers": webhook_headers,
                    "timeout": webhook_timeout
                }
            normalized_tools.append(normalized)
        else:
            # Direct format (already normalized)
            if "name" not in tool or "description" not in tool:
                return jsonify({"error": "tool missing name or description"}), 400
            # Apply global webhook if no implementation
            if "implementation" not in tool and webhook_url:
                tool["implementation"] = {
                    "type": "http_callback",
                    "url": webhook_url,
                    "method": webhook_method,
                    "headers": webhook_headers,
                    "timeout": webhook_timeout
                }
            normalized_tools.append(tool)
    
    tools = normalized_tools
    print(f"[playground/start] After normalization: {len(tools)} tools ready", file=sys.stderr, flush=True)
    
    room = payload.get("room") or f"play-{_uuid.uuid4().hex[:8]}"
    identity = f"user-{_uuid.uuid4().hex[:6]}"
    
    # Get room_config from frontend (official LiveKit pattern)
    room_config_payload = payload.get("room_config")

    try:
        data = _load_all()
        agent_conf = next((a for a in data.get("agents", []) if a.get("id") == agent_id), {})
        d = _load_defaults()
        lk_cfg = (agent_conf or {}).get("livekit", {}) if isinstance(agent_conf, dict) else {}
        lk_def = (d or {}).get("livekit", {})
        lk_url = str(lk_cfg.get("url") or lk_def.get("url") or os.getenv("LIVEKIT_URL", ""))
        lk_key = str(lk_cfg.get("api_key") or lk_def.get("api_key") or os.getenv("LIVEKIT_API_KEY", ""))
        lk_secret = str(lk_cfg.get("api_secret") or lk_def.get("api_secret") or os.getenv("LIVEKIT_API_SECRET", ""))
        if not (lk_url and lk_key and lk_secret):
            return jsonify({"error":"missing_livekit_env", "hint":"Set LIVEKIT_URL/KEY/SECRET in server env or in the agent's livekit section."}), 500
        
        token = lk_api.AccessToken(lk_key, lk_secret)
        token.with_identity(identity)
        token.with_name(identity)
        grants = lk_api.VideoGrants(
            room=room,
            room_join=True,
            can_publish=True,
            can_subscribe=True,
        )
        token.with_grants(grants)
        jwt = token.to_jwt()
        
        # Store tools in session cache before returning
        if tools:
            _playground_set_tools(room, tools)
            print(f"[playground] Stored {len(tools)} tools for room {room}", file=sys.stderr, flush=True)
        
        # Spawn agent worker on-demand for this playground session
        try:
            # Check if worker already exists
            if room not in _PLAYGROUND_PROCS:
                def _spawn_playground_worker():
                    try:
                        env = os.environ.copy()
                        env.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", os.getenv("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python"))
                        env.setdefault("PYTHONUNBUFFERED", "1")
                        env["RUN_AS_WORKER"] = "1"
                        env["PLAYGROUND_ROOM_NAME"] = str(room)
                        env["PLAYGROUND_FORCE_DIRECT"] = "1"
                        env.setdefault("PLAYGROUND_IDENTITY", f"agent-{_uuid.uuid4().hex[:6]}")
                        env["AGENT_PROFILE_ID"] = str(agent_id)
                        env["LIVEKIT_URL"] = lk_url
                        env["LIVEKIT_API_KEY"] = lk_key
                        env["LIVEKIT_API_SECRET"] = lk_secret
                        
                        # Inject OpenAI credentials from defaults if present
                        try:
                            openai_def = (d or {}).get("openai", {})
                            if openai_def.get("api_key"):
                                env.setdefault("OPENAI_API_KEY", str(openai_def.get("api_key")))
                            if openai_def.get("base_url"):
                                env.setdefault("OPENAI_BASE_URL", str(openai_def.get("base_url")))
                        except Exception:
                            pass
                        
                        env.setdefault("AGENT_NAME", f"playground-{agent_id}")
                        
                        # Provide CONFIG_API_BASE for tool resolution
                        try:
                            env.setdefault("CONFIG_API_BASE", os.getenv("CONFIG_API_BASE", f"http://localhost:{os.getenv('CONFIG_API_PORT','5057')}"))
                        except Exception:
                            pass
                        
                        agent_path = str((Path(__file__).parent / "agent.py").resolve())
                        
                        # Create log file
                        try:
                            logs_dir = Path(__file__).parent / "logs"
                            logs_dir.mkdir(parents=True, exist_ok=True)
                            ts = time.strftime("%Y%m%d_%H%M%S")
                            short = _uuid.uuid4().hex[:6]
                            log_fp = open(str(logs_dir / f"play_{room}_{ts}_{short}.log"), "wb")
                        except Exception:
                            log_fp = None
                        
                        if log_fp is not None:
                            proc = subprocess.Popen([sys.executable, agent_path], env=env, stdout=log_fp, stderr=subprocess.STDOUT, preexec_fn=_preexec_child)
                            _PLAYGROUND_LOGFILES[room] = log_fp
                        else:
                            proc = subprocess.Popen([sys.executable, agent_path], env=env, preexec_fn=_preexec_child)
                        
                        _PLAYGROUND_PROCS[room] = proc
                        print(f"[playground] Spawned worker for room {room} (PID: {proc.pid})", file=sys.stderr, flush=True)
                    except Exception as e:
                        print(f"[playground] Failed to spawn worker: {e}", file=sys.stderr, flush=True)
                
                # Spawn in background thread to avoid blocking response
                threading.Thread(target=_spawn_playground_worker, daemon=True).start()
        except Exception as e:
            print(f"[playground] Worker spawn error: {e}", file=sys.stderr, flush=True)
        
        return jsonify({"server_url": lk_url, "room": room, "token": jwt, "identity": identity})
    except Exception as e:
        return jsonify({"error": f"token_error: {e}"}), 500


@app.post("/playground/stop")
@require_auth()
def playground_stop():
    payload = request.json or {}
    room = payload.get("room")
    if not room:
        return jsonify({"error":"room_required"}), 400
    proc = _PLAYGROUND_PROCS.pop(room, None)
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
    logfp = _PLAYGROUND_LOGFILES.pop(room, None)
    if logfp is not None:
        try:
            logfp.flush(); logfp.close()
        except Exception:
            pass
    # Clear tools cache for this room
    _playground_clear_tools(room)
    print(f"[playground] Cleared tools cache for room {room}", file=sys.stderr, flush=True)
    return jsonify({"ok": True})


@app.get("/internal/playground/<room>/tools")
@require_internal()
def get_playground_tools(room: str):
    """Return tools configured for a playground session (agent worker use only)."""
    tools = _playground_get_tools(room)
    return jsonify({"tools": tools})


# ============================================================================
# NOTE: Worker management is handled by a separate supervisor process
# See agents_supervisor.py for automatic worker management
# ============================================================================

# Ensure the new playground routes are also available under API_PREFIX (e.g., /api)
_duplicate_routes_with_prefix(app, API_PREFIX)

if __name__ == "__main__":
    port = int(os.getenv("CONFIG_API_PORT", "5057"))
    app.run(host="0.0.0.0", port=port, debug=True)




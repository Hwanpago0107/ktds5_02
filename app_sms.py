"""FastAPI SMS webhook and analysis service (behavior preserved)."""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
import asyncio
from store import build_store_from_env
import os, json, requests, logging
try:
    from openai import AzureOpenAI
    _HAS_AZURE_OPENAI = True
except Exception:
    AzureOpenAI = None
    _HAS_AZURE_OPENAI = False

logger = logging.getLogger("app_sms")

app = FastAPI()

# Allow cross-origin for flexibility (safe to keep since Streamlit is same-origin in Azure)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

STORE = build_store_from_env()

# In-memory inbox (fallback)
INBOX: List[Dict] = []
_next_id = 1
_inbox_lock = asyncio.Lock()

# SSE subscribers
_subscribers: List[asyncio.Queue] = []

async def _broadcast(event: Dict) -> None:
    dead = []
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass

async def add_to_inbox(message: str, sender: Optional[str], receiver: Optional[str],
                       provider_message_id: Optional[str], received_at_iso: Optional[str],
                       trigger_auto: bool = True):
    global _next_id
    ts = None
    if received_at_iso:
        try:
            ts = datetime.fromisoformat(received_at_iso.replace("Z","+00:00")).isoformat()
        except Exception:
            ts = None
    async with _inbox_lock:
        row = {
            "id": _next_id,
            "message": message,
            "sender": sender,
            "receiver": receiver,
            "provider_message_id": provider_message_id,
            "received_at": ts or datetime.utcnow().isoformat(),
        }
        INBOX.append(row)
        try:
            if STORE is not None:
                STORE.add_sms(_next_id, {
                    "message": row["message"],
                    "sender": row.get("sender"),
                    "receiver": row.get("receiver"),
                    "provider_message_id": row.get("provider_message_id"),
                    "received_at": row.get("received_at"),
                    "created_at": datetime.utcnow().isoformat(),
                })
        except Exception:
            pass
        _next_id += 1
    try:
        await _broadcast({"type": "sms", "id": row["id"]})
    except Exception:
        pass
    if trigger_auto:
        try:
            _schedule_auto_analyze(message)
        except Exception:
            pass

# ---- Auto analysis backend (lightweight, server-side) ----

SEARCH_ENDPOINT   = os.getenv("SEARCH_ENDPOINT", "").rstrip("/")
SEARCH_INDEX      = os.getenv("SEARCH_INDEX", "kb-playbook")
SEARCH_KEY        = os.getenv("SEARCH_QUERY_KEY") or os.getenv("SEARCH_ADMIN_KEY")

AOAI_ENDPOINT     = (os.getenv("AZURE_OPENAI_ENDPOINT") or "").rstrip("/")
if AOAI_ENDPOINT.lower().endswith("/openai"):
    AOAI_ENDPOINT = AOAI_ENDPOINT[: -len("/openai")].rstrip("/")
AOAI_KEY          = os.getenv("AZURE_OPENAI_KEY")
AOAI_API_VERSION  = os.getenv("AOAI_API_VERSION", "2025-01-01-preview")
AOAI_API_EMBED_VERSION = os.getenv("AOAI_API_EMBED_VERSION", "2024-10-21")
AOAI_EMBED_DEPLOY = os.getenv("AOAI_DEPLOYMENT_EMBED", "text-embedding-3-small")
AOAI_CHAT_DEPLOY  = os.getenv("AOAI_DEPLOYMENT_CHAT", "gpt-4.1-mini")

# Infobip configuration
INFOBIP_HOST   = (os.getenv("INFOBIP_API_HOST") or os.getenv("INFOBIP_API_BASE") or "").strip().strip('/')
INFOBIP_APIKEY = (os.getenv("INFOBIP_API_KEY") or "").strip()
INFOBIP_SENDER = (os.getenv("INFOBIP_SENDER") or "InfoSMS").strip()

def _now_iso():
    try:
        return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        return datetime.utcnow().isoformat()

def _embed_texts(texts: List[str]) -> List[List[float]]:
    if _HAS_AZURE_OPENAI:
        try:
            client = AzureOpenAI(azure_endpoint=AOAI_ENDPOINT, api_key=AOAI_KEY, api_version=AOAI_API_EMBED_VERSION)
            resp = client.embeddings.create(model=AOAI_EMBED_DEPLOY, input=texts)
            return [d.embedding for d in resp.data]
        except Exception:
            pass
    url = f"{AOAI_ENDPOINT}/openai/deployments/{AOAI_EMBED_DEPLOY}/embeddings?api-version={AOAI_API_EMBED_VERSION}"
    headers = {"Content-Type": "application/json", "api-key": AOAI_KEY}
    r = requests.post(url, headers=headers, data=json.dumps({"input": texts}))
    r.raise_for_status()
    data = r.json()
    return [d["embedding"] for d in data["data"]]

def _hybrid_search(q_text: str, top=5, k=8, weight=1.2):
    q_vec = _embed_texts([q_text])[0]
    url = f"{SEARCH_ENDPOINT}/indexes('{SEARCH_INDEX}')/docs/search.post.search?api-version=2024-07-01"
    headers = {"Content-Type": "application/json", "api-key": SEARCH_KEY}
    body = {
        "search": q_text,
        "vectorQueries": [{
            "kind": "vector",
            "vector": q_vec,
            "fields": "vector",
            "k": k,
            "weight": weight
        }],
        "top": top,
        "select": "id,title,operator,direction,process,error_code,root_cause,initial_actions,diag_steps,escalation",
        "queryType": "semantic",
        "semanticConfiguration": "kb-semcfg",
        "vectorFilterMode": "preFilter"
    }
    r = requests.post(url, headers=headers, data=json.dumps(body))
    r.raise_for_status()
    return r.json()

SYSTEM_PROMPT = """KT 운영 메시지 자동 분석 시스템입니다.
아래 포맷으로 간결히 답변하세요.
[요약]
[초기조치 체크리스트]
[추가 정보]
[에스컬레이션]
[근거] KB-xxx - title (최대 3개)
"""

def _render_context_items(results, max_items=3):
    items = []
    for doc in results.get("value", [])[:max_items]:
        items.append({
            "id": doc.get("id"),
            "title": doc.get("title"),
            "root_cause": doc.get("root_cause"),
            "initial_actions": doc.get("initial_actions"),
            "diag_steps": doc.get("diag_steps"),
            "escalation": doc.get("escalation")
        })
    return items

def _chat_answer(sms_text: str, ctx_items: List[Dict]):
    if _HAS_AZURE_OPENAI:
        try:
            client = AzureOpenAI(azure_endpoint=AOAI_ENDPOINT, api_key=AOAI_KEY, api_version=AOAI_API_VERSION)
            resp = client.chat.completions.create(
                model=AOAI_CHAT_DEPLOY,
                messages=[{"role": "system", "content": SYSTEM_PROMPT},{"role":"user","content": json.dumps({"sms": sms_text, "top_kb": ctx_items}, ensure_ascii=False)}],
                temperature=0.2,
            )
            return resp.choices[0].message.content
        except Exception:
            pass
    url = f"{AOAI_ENDPOINT}/openai/deployments/{AOAI_CHAT_DEPLOY}/chat/completions?api-version={AOAI_API_VERSION}"
    headers = {"Content-Type": "application/json", "api-key": AOAI_KEY}
    payload = {"messages":[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content": json.dumps({"sms": sms_text, "top_kb": ctx_items}, ensure_ascii=False)}], "temperature":0.2}
    r = requests.post(url, headers=headers, data=json.dumps(payload))
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def _truncate_utf8(s: str, max_bytes: int) -> str:
    try:
        raw = s.encode('utf-8')
        if len(raw) <= max_bytes:
            return s
        cut = raw[:max_bytes]
        # avoid breaking multibyte sequence
        out = cut.decode('utf-8', errors='ignore')
        # add ellipsis if room
        ell = '…'
        if len(out.encode('utf-8')) + len(ell.encode('utf-8')) <= max_bytes:
            out = out + ell
        return out
    except Exception:
        return s[:max_bytes]

def _ai_summarize_short(text: str, ctx_items: List[Dict], max_bytes: int = 180) -> Optional[str]:
    try:
        if not text:
            return None
        prompt = (
            "아래 내용을 수신자가 원인 파악과 즉시 조치에 도움을 받을 수 있도록, "
            "한국어의 완전한 문장 1~2개로 요약하세요. 불필요한 접두사/대괄호/개행 없이 핵심만 쓰고, "
            f"UTF-8 기준 {max_bytes}바이트를 절대 넘기지 마세요.\n\n"
            f"답변 본문:\n{text}\n"
        )
        if _HAS_AZURE_OPENAI and AOAI_ENDPOINT and AOAI_KEY and AOAI_API_VERSION:
            client = AzureOpenAI(azure_endpoint=AOAI_ENDPOINT, api_key=AOAI_KEY, api_version=AOAI_API_VERSION)
            resp = client.chat.completions.create(
                model=AOAI_CHAT_DEPLOY,
                messages=[
                    {"role":"system","content":"너는 운영 현장 담당자를 위한 초간결 알림 문장을 작성한다. 원인과 즉시 취할 조치 하나를 포함해 완전한 문장으로 답한다."},
                    {"role":"user","content":prompt}
                ],
                temperature=0.2,
                max_tokens=160,
            )
            cand = (resp.choices[0].message.content or '').strip()
            if cand:
                return _truncate_utf8(cand, max_bytes)
    except Exception:
        return None
    return None

def _build_summary_sms(answer_text: str, ctx_items: List[Dict], max_bytes: int = 180) -> str:
    try:
        # Try AI-constrained summary first
        ai = _ai_summarize_short(answer_text, ctx_items, max_bytes=max_bytes)
        if ai:
            return ai
        # Fallback: truncate original answer
        base = (answer_text or '').strip()
        if not base and ctx_items:
            base = (ctx_items[0].get('title') or ctx_items[0].get('id') or '').strip()
        return _truncate_utf8(base, max_bytes)
    except Exception:
        return _truncate_utf8((answer_text or '').strip(), max_bytes)

def _send_infobip_sms(to_msisdn: str, text: str) -> Optional[Dict]:
    to = (to_msisdn or "").strip()
    if not to or not INFOBIP_HOST or not INFOBIP_APIKEY:
        return None
    url = f"https://{INFOBIP_HOST}/sms/2/text/advanced"
    headers = {
        "Authorization": f"App {INFOBIP_APIKEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "messages": [
            {
                "destinations": [{"to": to}],
                "from": INFOBIP_SENDER,
                "text": text,
            }
        ]
    }
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
    try:
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def _schedule_auto_analyze(message: str):
    try:
        asyncio.get_event_loop().create_task(_auto_analyze(message))
    except RuntimeError:
        # In case no running loop (sync context), run in background thread
        asyncio.run(_auto_analyze(message))

from typing import Optional

def _auto_analyze_blocking(message: str) -> Optional[str]:
    try:
        text = (message or "").strip()
        if not text:
            return
        n = {"raw": text}
        hits = _hybrid_search(text, top=5, k=8, weight=1.2)
        ctx_items = _render_context_items(hits, max_items=3)
        answer = _chat_answer(text, ctx_items)
        rec_out = {
            "sms": text,
            "normalized": n,
            "hits": hits,
            "context": ctx_items,
            "answer": answer,
            "ts": _now_iso(),
        }
        aid: Optional[str] = None
        try:
            if STORE is not None:
                aid = STORE.save_analysis(rec_out)
        except Exception:
            pass
        try:
            if _NOTIFY_MSISDN:
                sms_text = _build_summary_sms(answer, ctx_items)
                _send_infobip_sms(_NOTIFY_MSISDN, sms_text)
        except Exception:
            pass
        return aid
    except Exception:
        return None

async def _auto_analyze(message: str) -> None:
    try:
        # Offload blocking network calls to a worker thread to avoid blocking the event loop
        aid = await asyncio.to_thread(_auto_analyze_blocking, message)
        try:
            if aid:
                await _broadcast({"type": "analysis", "id": aid})
        except Exception:
            pass
    except Exception:
        pass

@app.post("/sms")
async def inbound_sms(request: Request):
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        body = await request.json()
        events = body if isinstance(body, list) else [body]
        for e in events:
            results = e.get("results") if isinstance(e, dict) else None
            if results and isinstance(results, list):
                for item in results:
                    await add_to_inbox(
                        message=item.get("text") or item.get("message"),
                        sender=item.get("from"),
                        receiver=item.get("to"),
                        provider_message_id=item.get("messageId"),
                        received_at_iso=item.get("receivedAt"),
                        trigger_auto=True
                    )
        return PlainTextResponse("OK")
    if "application/x-www-form-urlencoded" in ctype:
        form = await request.form()
        await add_to_inbox(
            message=form.get("text") or form.get("message") or form.get("Body"),
            sender=form.get("from") or form.get("sender") or form.get("From"),
            receiver=form.get("to") or form.get("receiver") or form.get("To"),
            provider_message_id=form.get("messageId") or form.get("message_id") or form.get("MessageSid"),
            received_at_iso=form.get("receivedAt") or form.get("sentAt"),
            trigger_auto=False
        )
        return PlainTextResponse("OK")
    raw = (await request.body())[:1000]
    await add_to_inbox(message=str(raw), sender=None, receiver=None,
                       provider_message_id=None, received_at_iso=None)
    return PlainTextResponse("OK")

@app.get("/", include_in_schema=False)
async def root():
    # Simple landing page to avoid FastAPI default 404 at '/'
    html = (
        "<html><head><title>KTDS SMS API</title></head><body>"
        "<h2>KTDS SMS API</h2>"
        "<p>Service is running.</p>"
        "<ul>"
        "<li><a href=\"/docs\">OpenAPI Docs</a></li>"
        "<li><a href=\"/healthz\">Health Check</a></li>"
        "<li><a href=\"/api/sms/recent\">Recent SMS JSON</a></li>"
        "</ul>"
        "</body></html>"
    )
    return HTMLResponse(content=html, status_code=200)

@app.get("/api/sms/recent")
async def get_recent(limit: int = 50, since_id: int = 0):
    if STORE is not None:
        try:
            items = STORE.get_sms_recent(since_id=since_id, limit=min(int(limit), 500))
            return JSONResponse(items)
        except Exception:
            pass
    async with _inbox_lock:
        rows = [x for x in INBOX if x["id"] > since_id]
        rows = sorted(rows, key=lambda r: r["id"], reverse=True)[: min(limit, 500)]
    return JSONResponse(rows)

@app.get("/api/sms/stream")
async def sms_stream(request: Request):
    async def event_gen():
        q: asyncio.Queue = asyncio.Queue()
        _subscribers.append(q)
        try:
            yield "event: ping\ndata: connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=30)
                    sms_id = evt.get("id") if isinstance(evt, dict) else None
                    payload = str(sms_id) if sms_id is not None else "1"
                    yield f"event: sms\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: keep-alive\n\n"
        finally:
            try:
                _subscribers.remove(q)
            except ValueError:
                pass
    return StreamingResponse(event_gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Access-Control-Allow-Origin": "*",
        "X-Accel-Buffering": "no",
    })

# --- Notify recipient config API ---
@app.get("/api/notify/config")
async def get_notify_config():
    return JSONResponse({
        "recipient": _NOTIFY_MSISDN,
        "sender": INFOBIP_SENDER,
        "host": INFOBIP_HOST,
        "enabled": bool(_NOTIFY_MSISDN and INFOBIP_HOST and INFOBIP_APIKEY),
    })

@app.post("/api/notify/config")
async def set_notify_config(request: Request):
    try:
        ctype = (request.headers.get("content-type") or "").lower()
        rec = None
        if "application/json" in ctype:
            body = await request.json()
            rec = (body or {}).get("recipient")
        else:
            form = await request.form()
            rec = form.get("recipient")
        global _NOTIFY_MSISDN
        _NOTIFY_MSISDN = (rec or "").strip()
        return JSONResponse({"ok": True, "recipient": _NOTIFY_MSISDN})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

async def _init_next_id_from_store() -> None:
    global _next_id
    try:
        if STORE is not None:
            max_id = STORE.get_sms_max_id()
            if isinstance(max_id, int) and max_id >= 1:
                _next_id = max_id + 1
    except Exception:
        pass

import asyncio as _asyncio
_asyncio.get_event_loop().create_task(_init_next_id_from_store())

# Runtime-configurable notify recipient (default from env)
_NOTIFY_MSISDN = (os.getenv("NOTIFY_RECIPIENT") or "").strip()

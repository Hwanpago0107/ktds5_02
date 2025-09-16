# step3_rag_query.py
import os, re, json, time, requests, math
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from store import build_store_from_env
import streamlit as st
from streamlit.components.v1 import html as st_html
try:
    from openai import AzureOpenAI  # Azure OpenAI SDK client
    _HAS_AZURE_OPENAI = True
except Exception:
    AzureOpenAI = None
    _HAS_AZURE_OPENAI = False

# Load environment first so WEBHOOK_API_BASE is available
load_dotenv()

# Prefer env var for webapp; if not set, default to local FastAPI port in-container.
# Browser JS uses relative URLs; server-side Python (requests) needs absolute URL.
_RAW_API_BASE = (os.getenv("WEBHOOK_API_BASE") or "").strip().rstrip("/")
API_BASE = _RAW_API_BASE if _RAW_API_BASE else "http://127.0.0.1:8000"
STORE = build_store_from_env()

# ---- Helpers ----
def fmt_recv_at(s: str) -> str:
    """Format ISO timestamps like 2025-09-15T01:33:47.645000+00:00 into a compact local string.
    - If looks like ISO with timezone, convert to KST (UTC+9) and format as 'YYYY-MM-DD HH:MM'
    - If already a simple string (no 'T'/'Z'/offset), return as-is
    """
    try:
        if not s:
            return ""
        if 'T' not in s and 'Z' not in s and (len(s) <= 10 or '+' not in s[10:]):
            return s
        s2 = s.replace('Z', '+00:00')
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        kst = timezone(timedelta(hours=9))
        dt = dt.astimezone(kst)
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return s

def now_iso_utc_z() -> str:
    try:
        return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return time.strftime('%Y-%m-%d %H:%M:%S')

def _parse_dt_safe(s: str):
    try:
        if not s:
            return None
        s2 = s.replace('Z', '+00:00')
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        try:
            return datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        except Exception:
            return None

# ===== 환경변수 =====
SEARCH_ENDPOINT   = os.environ["SEARCH_ENDPOINT"].rstrip("/")
SEARCH_INDEX      = os.getenv("SEARCH_INDEX", "kb-playbook")
SEARCH_KEY        = os.getenv("SEARCH_QUERY_KEY") or os.getenv("SEARCH_ADMIN_KEY")  # 런타임은 query key 권장

AOAI_ENDPOINT     = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
# Avoid duplicate path like '/openai/openai' if env already includes '/openai'
if AOAI_ENDPOINT.lower().endswith("/openai"):
    AOAI_ENDPOINT = AOAI_ENDPOINT[: -len("/openai")].rstrip("/")
AOAI_KEY          = os.environ["AZURE_OPENAI_KEY"]
AOAI_API_VERSION  = os.getenv("AOAI_API_VERSION", "2025-01-01-preview")
AOAI_API_EMBED_VERSION  = os.getenv("AOAI_API_EMBED_VERSION", "2024-10-21")
AOAI_EMBED_DEPLOY = os.getenv("AOAI_DEPLOYMENT_EMBED", "text-embedding-3-small")
AOAI_CHAT_DEPLOY  = os.getenv("AOAI_DEPLOYMENT_CHAT",  "gpt-4.1-mini")

EMBEDDING_DIMS    = int(os.getenv("EMBEDDING_DIMS", "1536"))  # text-embedding-3-small = 1536

# Initialize Azure OpenAI SDK clients when available
CHAT_CLIENT = None
EMBED_CLIENT = None
if _HAS_AZURE_OPENAI:
    try:
        CHAT_CLIENT = AzureOpenAI(azure_endpoint=AOAI_ENDPOINT, api_key=AOAI_KEY, api_version=AOAI_API_VERSION)
    except Exception:
        CHAT_CLIENT = None
    try:
        EMBED_CLIENT = AzureOpenAI(azure_endpoint=AOAI_ENDPOINT, api_key=AOAI_KEY, api_version=AOAI_API_EMBED_VERSION)
    except Exception:
        EMBED_CLIENT = None

# ====== 정규식 & 파서 ======
def rx(p):  # 미리 컴파일 + 대소문자 무시
    return re.compile(p, re.IGNORECASE)

PROCESS_SYNONYMS = [
    (rx(r"(신규\s*개통|개통)"), "ACTIVATION"),
    (rx(r"(HLR|HSS|반영\s*지연|프로비|프로비저닝)"), "PROVISIONING"),
    (rx(r"(모바일\s*AP|모바일AP|계약)"), "CONTRACT"),
    (rx(r"(결합(상품)?|가족결합|인터넷\s*결합)"), "BUNDLE"),
    (rx(r"(요금제|요금)"), "RATEPLAN"),
    (rx(r"(개통취소|취소)"), "CANCELLATION"),
    (rx(r"(USIM|ICCID|IMSI)"), "USIM"),
    (rx(r"(본인인증|KYC|신원)"), "IDENTITY"),
    (rx(r"(번호\s*변경)"), "CHANGE_NUMBER"),
    (rx(r"(부가서비스|부가)"), "ADDON"),
    (rx(r"(사전\s*동의|사전동의|pre[\s_\-]*auth)"), "PRE_AUTH"),
    (rx(r"(인증|auth)"), "AUTH"),
]
DIRECTION_SYNONYMS = [
    (rx(r"(포트\s*아웃|포트아웃|port\s*out)"), "PORT_OUT"),
    (rx(r"(포트\s*인|포트인|port\s*in)"), "PORT_IN"),
]
OPERATOR_SYNONYMS = [
    (rx(r"\bKT\b|케이티"), "KT"),
    (rx(r"\bSKT\b|에스케이티"), "SKT"),
    (rx(r"LGU\+|엘지유플러스|U\+"), "LGU+"),
    (rx(r"MVNO|알뜰"), "MVNO"),
]
ERROR_CODE_PAT = re.compile(r"\b([A-Z]{2}\d{4})\b", re.IGNORECASE)
COUNT_PAT      = re.compile(r"(?:\uAC74\uC218)\\s*[:\\-]?\\s*(\\d+)")
HOURS_PAT      = re.compile(r"(\\d+)\\s*(?:\\uC2DC\\uAC04)")
MINS_PAT       = re.compile(r"(\\d+)\\s*(?:\\uBD84)")



def normalize_sms(text: str):
    t = text.strip()
    res = {
        "operator": None, "direction": None, "process": None,
        "error_code": None, "count": None, "window_minutes": None, "raw": t
    }
    for pat, val in OPERATOR_SYNONYMS:
        if pat.search(t): res["operator"] = val; break
    for pat, val in DIRECTION_SYNONYMS:
        if pat.search(t): res["direction"] = val; break
    for pat, val in PROCESS_SYNONYMS:
        if pat.search(t): res["process"] = val; break
    m = ERROR_CODE_PAT.search(t)
    if m: res["error_code"] = m.group(1).upper()
    mc = COUNT_PAT.search(t)
    if mc: res["count"] = int(mc.group(1))
    minutes = 0
    mh = HOURS_PAT.search(t)
    if mh: minutes += int(mh.group(1)) * 60
    mm = MINS_PAT.search(t)
    if mm: minutes += int(mm.group(1))
    res["window_minutes"] = minutes or None
    if not res["error_code"]:
        if res["process"] == "ACTIVATION" and ("실패" in t or "오류" in t):
            res["error_code"] = "ACT_FAIL"
        elif res["process"] == "PROVISIONING" and ("반영 지연" in t or "지연" in t):
            res["error_code"] = "HLR_DELAY"
    return res

# ====== 임베딩 & 검색 ======
def embed_texts(texts, retries=5, backoff=1.5):
    # Prefer SDK (AzureOpenAI) when available; fallback to raw requests
    last = None
    for i in range(retries):
        try:
            if EMBED_CLIENT is not None:
                try:
                    resp = EMBED_CLIENT.embeddings.create(model=AOAI_EMBED_DEPLOY, input=texts)
                    return [d.embedding for d in resp.data]
                except Exception as e:
                    msg = str(e)
                    if "Error code: 404" in msg:
                        raise RuntimeError(
                            f"Azure OpenAI embedding deployment not found: AOAI_DEPLOYMENT_EMBED='{AOAI_EMBED_DEPLOY}'. "
                            f"Check deployment name and AZURE_OPENAI_ENDPOINT. Raw: {msg}"
                        )
                    last = msg
                    time.sleep(backoff * (i + 1)); continue
            else:
                url = f"{AOAI_ENDPOINT}/openai/deployments/{AOAI_EMBED_DEPLOY}/embeddings?api-version={AOAI_API_EMBED_VERSION}"
                headers = {"Content-Type": "application/json", "api-key": AOAI_KEY}
                payload = {"input": texts}
                r = requests.post(url, headers=headers, data=json.dumps(payload))
                if r.status_code == 200:
                    data = r.json()
                    return [d["embedding"] for d in data["data"]]
                last = (r.status_code, r.text)
                if r.status_code in (404, 429, 500, 502, 503, 504):
                    time.sleep(backoff * (i + 1)); continue
                break
        except Exception as e:
            last = str(e)
            time.sleep(backoff * (i + 1)); continue
    raise RuntimeError(f"Embedding failed after retries: last={last}")

def build_filter(n):
    fs = []
    if n.get("operator"):
        fs.append(f"operator/any(o: o eq '{n['operator']}')")
    if n.get("direction"):
        fs.append(f"direction eq '{n['direction']}'")
    if n.get("process"):
        fs.append(f"process eq '{n['process']}'")
    if n.get("error_code"):
        fs.append(f"(error_code eq '{n['error_code']}' or error_code eq 'ANY')")
    return " and ".join(fs) if fs else None

def hybrid_search(n, top=5, k=8, weight=1.2):
    tokens = [n.get("operator") or "", n.get("direction") or "", n.get("process") or "", n.get("error_code") or ""]
    q_text = (" ".join([t for t in tokens if t]).strip() + " " + n["raw"]).strip()
    q_vec  = embed_texts([q_text])[0]

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
    f = build_filter(n)
    if f: body["filter"] = f
    r = requests.post(url, headers=headers, data=json.dumps(body))
    if r.status_code >= 300:
        raise RuntimeError(f"Search failed: {r.status_code} {r.text}")
    return r.json()

SYSTEM_PROMPT = """너는 KT 고객운영팀의 관제/장애 대응 보조 분석가다.
반드시 근거(KB id, title)를 포함하고, 과장하지 말고 모르는 것은 모른다고 답한다.
출력 섹션은 다음 순서를 유지한다:
[원인]
[초동조치 체크리스트]
[추가 진단]
[에스컬레이션]
[근거] KB-xxx - title (최대 3개)
"""

def render_context_items(results, max_items=3):
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

def generate_answer(sms_text, n, search_json):
    ctx_items = render_context_items(search_json, max_items=3)
    user_msg = {
        "sms": sms_text,
        "normalized": n,
        "top_kb": ctx_items
    }
    if CHAT_CLIENT is not None:
        try:
            resp = CHAT_CLIENT.chat.completions.create(
                model=AOAI_CHAT_DEPLOY,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(user_msg, ensure_ascii=False)}
                ],
                temperature=0.2,
            )
            return resp.choices[0].message.content, ctx_items
        except Exception as e:
            msg = str(e)
            if "Error code: 404" in msg:
                raise RuntimeError(
                    f"Azure OpenAI chat deployment not found: AOAI_DEPLOYMENT_CHAT='{AOAI_CHAT_DEPLOY}'. "
                    f"Check deployment name and AZURE_OPENAI_ENDPOINT. Raw: {msg}"
                )
            raise
    else:
        url = f"{AOAI_ENDPOINT}/openai/deployments/{AOAI_CHAT_DEPLOY}/chat/completions?api-version={AOAI_API_VERSION}"
        headers = {"Content-Type": "application/json", "api-key": AOAI_KEY}
        payload = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_msg, ensure_ascii=False)}
            ],
            "temperature": 0.2
        }
        r = requests.post(url, headers=headers, data=json.dumps(payload))
        if r.status_code >= 300:
            hint = ""
            if r.status_code == 404:
                hint = (
                    f" | Hint: Azure OpenAI deployment not found. "
                    f"Check AOAI_DEPLOYMENT_CHAT='{AOAI_CHAT_DEPLOY}' and AZURE_OPENAI_ENDPOINT='{AOAI_ENDPOINT}'."
                )
            raise RuntimeError(f"Chat failed: {r.status_code} {r.text}{hint}")
        return r.json()["choices"][0]["message"]["content"], ctx_items



def pull_from_inbox():
    """FastAPI INBOX에서 새 메시지를 가져와 sms_records에 추가"""
    try:
        base = (API_BASE or "").rstrip("/")
        url = f"{base}/api/sms/recent?since_id={st.session_state.get('last_seen_id', 0)}&limit=100"
        # params = {"since_id": int(st.session_state.get("last_seen_id", 0) or 0), "limit": 100}
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return 0
        # 최신 id 갱신 & 메시지 추가(오래된 것부터 append)
        for row in sorted(data, key=lambda x: x["id"]):
            txt = row.get("message") or ""
            if txt.strip():
                recv_at = row.get("received_at") or now_iso_utc_z()
                st.session_state.sms_records.append({
                    "message": txt.strip(),
                    "received_at": recv_at,
                })
            st.session_state["last_seen_id"] = max(
                st.session_state.get("last_seen_id", 0), int(row.get("id", 0))
            )
        return len(data)
    except requests.exceptions.HTTPError as he:
        resp = getattr(he, "response", None)
        detail = f"{resp.status_code} {resp.reason}" if resp is not None else str(he)
        body = (resp.text[:300] if getattr(resp, "text", None) else "")
        st.toast(f"수신 동기화 실패: {detail} | {body}", icon="⚠️")
        return 0
    except Exception as e:
        st.toast(f"수신 동기화 실패: {e}", icon="⚠️")
        return 0# ====== Streamlit UI ======
st.set_page_config(page_title="KT 고객운영 SMS 분석", page_icon="📨", layout="wide")
st.title("📞 KT 고객운영팀 관제 SMS 분석")

# 초기 세션 상태
if "sms_records" not in st.session_state:
    st.session_state.sms_records = []
if "analysis_history" not in st.session_state:
    st.session_state.analysis_history = []

# 기존 문자열 기록 마이그레이션 → {message, received_at}
if st.session_state.sms_records and isinstance(st.session_state.sms_records[0], str):
    now_ts = now_iso_utc_z()
    st.session_state.sms_records = [
        {"message": m, "received_at": now_ts} if isinstance(m, str) else m
        for m in st.session_state.sms_records
    ]

# last_seen_id 초기화
if "last_seen_id" not in st.session_state:
    st.session_state.last_seen_id = 0

# --- SMS: 기록 & 입력 ---
st.subheader("SMS 기록")

pull_from_inbox()

# Keep chronological order: older -> newer (newest at bottom)
try:
    st.session_state.sms_records = sorted(
        st.session_state.sms_records,
        key=lambda r: _parse_dt_safe(r.get("received_at") if isinstance(r, dict) else None) or datetime.min.replace(tzinfo=timezone.utc)
    )
except Exception:
    pass

# SSE 리스너: 서버에 새 SMS가 오면 자동 새로고침(재연결 + 폴백 폴링)
try:
    last_id = int(st.session_state.get("last_seen_id", 0) or 0)
    # 폴링 간격 확보
    try:
        _default_poll_ms = int(os.getenv('POLL_INTERVAL_MS', '5000'))
    except Exception:
        _default_poll_ms = 5000
    poll_ms = int(st.session_state.get('poll_interval_ms', _default_poll_ms))

    # 중요: 브라우저는 same-origin으로만 접근해야 하므로 절대 URL 대신 경로만 사용
    sse_url = "/api/sms/stream"
    poll_url = f"/api/sms/recent?limit=1&since_id={last_id}"

    script = """
            <script>
            (function() {
                var pollIntervalMs = %POLL_MS%;
                var base = '%BASE%';
                var sinceId = %SINCE_ID%;
                var sseUrl = '%SSE_URL%';
                var pollUrl = '%POLL_URL%';
                function reloadPage() {
                    try { if (window && window.top) { window.top.location.reload(); } else { window.location.reload(); } }
                    catch(e) { window.location.reload(); }
                }
                function startPolling(){
                    setInterval(function() {
                        fetch(pollUrl, {
                            cache: 'no-store', mode: 'cors', credentials: 'omit',
                            headers: { 'Accept': 'application/json', 'ngrok-skip-browser-warning': 'true' }
                        })
                        .then(function(r){
                            if (!r.ok) return [];
                            var ct = (r.headers.get('content-type') || '').toLowerCase();
                            if (ct.indexOf('application/json') === -1) { return []; }
                            return r.json();
                        })
                        .then(function(data){ if (Array.isArray(data) && data.length > 0) { reloadPage(); } })
                        .catch(function(err){ console.debug('poll error', err); });
                    }, pollIntervalMs);
                }
                // Always start polling as a reliable fallback (low frequency)
                try { startPolling(); } catch(e) { console.debug('poll start failed', e); }
                if (window.EventSource) {
                    try {
                        var es = new EventSource(sseUrl);
                        es.addEventListener('sms', function(e){ reloadPage(); });
                        es.addEventListener('analysis', function(e){ reloadPage(); });
                        es.addEventListener('ping', function(e){});
                        es.onerror = function(e){ console.debug('sse error, fallback to poll', e); try { es.close(); } catch(_){} startPolling(); };
                    } catch(e) {
                        console.debug('sse init failed, fallback to poll', e); startPolling();
                    }
                } else {
                    startPolling();
                }
            })();
            </script>
    """.replace("%POLL_MS%", str(poll_ms)) \
       .replace("%SINCE_ID%", str(last_id)) \
       .replace("%SSE_URL%", sse_url) \
       .replace("%POLL_URL%", poll_url)
    st_html(script, height=0)
except Exception:
    pass


with st.sidebar:
    st.markdown("### 수신 동기화")
    # 폴링 간격(ms) 설정
    try:
        _default_poll_ms = int(os.getenv('POLL_INTERVAL_MS', '5000'))
    except Exception:
        _default_poll_ms = 5000
    st.session_state['poll_interval_ms'] = int(
        st.number_input(
            '자동 갱신(폴링) 간격(ms)',
            min_value=1000, max_value=1000000, step=500,
            value=int(st.session_state.get('poll_interval_ms', _default_poll_ms)),
        )
    )
    API_BASE = (st.text_input("Webhook API Base URL", API_BASE, help="예: https://<fastapi-ngrok>.ngrok-free.app").strip().rstrip("/"))
    if st.button("지금 불러오기"):
        added = pull_from_inbox()
        st.success(f"신규 SMS {added}건 불러옴")

with st.container(border=True):
    # 최근 기록 10개 표시 + 개별 분석 버튼
    for i, rec in enumerate(sorted(st.session_state.sms_records, key=lambda r: _parse_dt_safe(r.get("received_at") if isinstance(r, dict) else None) or datetime.min.replace(tzinfo=timezone.utc))[-10:], 1):
        cols = st.columns([8, 1])
        with cols[0]:
            with st.chat_message("user"):
                ts = fmt_recv_at(rec.get("received_at")) if isinstance(rec, dict) else ""
                msg_text = rec.get("message") if isinstance(rec, dict) else str(rec)
                st.write((f"{ts}  {msg_text}").strip())
        with cols[1]:
            if st.button("분석", key=f"analyze_{i}"):
                target_sms = (rec.get("message") if isinstance(rec, dict) else str(rec))
                try:
                    with st.spinner("정규화/검색/응답 생성 중..."):
                        n = normalize_sms(target_sms)
                        hits = hybrid_search(n, top=5, k=8, weight=1.3)
                        answer, ctx_items = generate_answer(target_sms, n, hits)
                    rec_out = {
                        "sms": target_sms,
                        "normalized": n,
                        "hits": hits,
                        "context": ctx_items,
                        "answer": answer,
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    if STORE is not None:
                        STORE.save_analysis(rec_out)
                    else:
                        st.session_state.analysis_history.insert(0, rec_out)
                    st.session_state["analysis_success_msg"] = "분석 완료! 아래 이력에서 확인하세요."
                except Exception as e:
                    st.error(f"분석 오류: {e}")

    # 새 SMS 입력
    new_sms = st.text_area("새 SMS 입력", height=80, placeholder="예) 건수:10, 최근 10분 내 포트아웃 번호이동 사전의 BF1099 ...")
    cols = st.columns([1, 1, 6])
    with cols[0]:
        add_btn = st.button("기록 추가")

    if add_btn and new_sms.strip():
        # Persist via backend instead of only session_state to survive full reloads
        st.success("SMS 기록이 추가되었습니다.")

        # Persist to backend so refresh/SSE keeps the entry
        try:
            base = (API_BASE or "").rstrip("/")
            payload = { 'text': new_sms.strip(), 'receivedAt': now_iso_utc_z() }
            requests.post(f"{base}/sms", data=payload, timeout=10)
        except Exception:
            pass
        # Trigger immediate UI refresh in-session
        try:
            st.rerun()
        except Exception:
            pass

    if st.session_state.get("analysis_success_msg"):
        st.success(st.session_state.analysis_success_msg)

with st.sidebar:
    st.markdown("---")
    st.markdown("### SMS (Infobip)")
    try:
        _sbase = (API_BASE or "http://127.0.0.1:8000").rstrip("/")
        conf = requests.get(f"{_sbase}/api/notify/config", timeout=10)
        cur_rec = ""
        if conf.ok:
            j = conf.json()
            cur_rec = (j.get("recipient") or "").strip()
    except Exception:
        cur_rec = ""
    new_rec = st.text_input("수신자", cur_rec, help="e.g., 821011122233")
    if st.button("저장"):
        try:
            _sbase = (API_BASE or "http://127.0.0.1:8000").rstrip("/")
            r = requests.post(f"{_sbase}/api/notify/config", data={"recipient": new_rec.strip()}, timeout=10)
            if r.ok:
                st.success("저장 완료")
            else:
                st.error(f"저장 실패: {r.text[:200]}")
        except Exception as e:
            st.error(f"저장 실패: {e}")

# --- 분석 이력 (페이지당 5개) ---
page_size = 5
if STORE is not None:
    st.subheader("분석 이력")
    if "history_page" not in st.session_state:
        st.session_state.history_page = 1
    items, total = STORE.get_analysis_page(st.session_state.history_page, page_size)
    total_pages = max(1, (total + page_size - 1) // page_size)
    st.session_state.history_page = max(1, min(st.session_state.history_page, total_pages))

    buttons_count = min(total_pages, 10)
    cols = st.columns([1] * buttons_count + [10])
    for idx in range(buttons_count):
        p = idx + 1
        with cols[idx]:
            if st.button(str(p), key=f"hist_page_{p}"):
                st.session_state.history_page = p

    if total == 0:
        st.info("아직 분석 이력이 없습니다. 위에서 SMS를 입력하고 분석을 실행해보세요.")
    else:
        start = (st.session_state.history_page - 1) * page_size
        for i, rec in enumerate(items, start + 1):
            ts_kst = fmt_recv_at(rec.get("ts")) if isinstance(rec, dict) else ""
            with st.expander(f"{i}. {ts_kst} | {rec['sms'][:50]}..."):
                c1, c2 = st.columns([3, 2])
                with c1:
                    st.markdown("**정규화 결과**")
                    st.json(rec.get("normalized"))
                    st.markdown("**RAG 답변**")
                    with st.chat_message("assistant"):
                        st.write(rec.get("answer"))
                with c2:
                    st.markdown("**Top KB 근거(최대 3)**")
                    if rec.get("context"):
                        for item in rec.get("context"):
                            st.markdown(f"- **{item.get('id','')}** · {item.get('title','')}")
                    else:
                        st.write("근거 없음")
                    st.markdown("**검색 Raw 결과**")
                    st.json(rec.get("hits"))
else:
    st.subheader("분석 이력")
    if not st.session_state.analysis_history:
        st.info("아직 분석 이력이 없습니다. 위에서 SMS를 입력하고 **분석**을 눌러보세요.")
    else:
        total = len(st.session_state.analysis_history)
        total_pages = max(1, (total + page_size - 1) // page_size)
        if "history_page" not in st.session_state:
            st.session_state.history_page = 1
        st.session_state.history_page = max(1, min(st.session_state.history_page, total_pages))

        buttons_count = min(total_pages, 10)
        cols = st.columns([1] * buttons_count + [10])
        for idx in range(buttons_count):
            p = idx + 1
            with cols[idx]:
                if st.button(str(p), key=f"hist_page_{p}"):
                    st.session_state.history_page = p

        start = (st.session_state.history_page - 1) * page_size
        end = min(start + page_size, total)
        page_items = st.session_state.analysis_history[start:end]
        for i, rec in enumerate(page_items, start + 1):
            ts_kst = fmt_recv_at(rec.get("ts")) if isinstance(rec, dict) else ""
            with st.expander(f"{i}. {ts_kst} | {rec['sms'][:50]}..."):
                c1, c2 = st.columns([3, 2])
                with c1:
                    st.markdown("**정규화 결과**")
                    st.json(rec["normalized"])
                    st.markdown("**RAG 답변**")
                    with st.chat_message("assistant"):
                        st.write(rec["answer"])
                with c2:
                    st.markdown("**Top KB 근거(최대 3)**")
                    if rec.get("context"):
                        for item in rec["context"]:
                            st.markdown(f"- **{item['id']}** · {item['title']}")
                    else:
                        st.write("근거 없음")
                    st.markdown("**검색 Raw 결과**")
                    st.json(rec["hits"])

import os, json, time, math, requests
from dotenv import load_dotenv

# ========== Load .env ==========
load_dotenv()
SEARCH_ENDPOINT   = os.environ["SEARCH_ENDPOINT"].rstrip("/")
SEARCH_ADMIN_KEY  = os.environ["SEARCH_ADMIN_KEY"]
INDEX_NAME        = os.getenv("SEARCH_INDEX", "kb-playbook")

AOAI_ENDPOINT     = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
AOAI_KEY          = os.environ["AZURE_OPENAI_KEY"]
AOAI_EMBED_DEPLOY = os.environ.get("AOAI_DEPLOYMENT_EMBED", "text-embedding-3-small")
AOAI_API_VERSION  = os.environ.get("AOAI_API_VERSION", "2024-10-21")

KB_JSON_PATH      = os.getenv("KB_JSON_PATH", "kb_playbook_sample_v2.json")

# ---- 임베딩 차원: text-embedding-3-small = 1536, text-embedding-3-large = 3072 ----
EMBEDDING_DIMS = 1536

# ========== Helpers ==========
def _headers(admin=True):
    h = {"Content-Type": "application/json"}
    if admin:
        h["api-key"] = SEARCH_ADMIN_KEY
    return h

def index_exists():
    url = f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}?api-version=2024-07-01"
    r = requests.get(url, headers=_headers())
    return r.status_code == 200

def create_index():
    url = f"{SEARCH_ENDPOINT}/indexes?api-version=2024-07-01"
    body = {
        "name": INDEX_NAME,
        "fields": [
            {"name":"id","type":"Edm.String","key":True,"filterable":True},
            {"name":"title","type":"Edm.String","searchable":True},
            {"name":"operator","type":"Collection(Edm.String)","filterable":True,"facetable":True},
            {"name":"direction","type":"Edm.String","filterable":True},
            {"name":"process","type":"Edm.String","filterable":True},
            {"name":"error_code","type":"Edm.String","filterable":True},
            {"name":"symptoms","type":"Edm.String","searchable":True},
            {"name":"root_cause","type":"Edm.String","searchable":True},
            {"name":"initial_actions","type":"Edm.String","searchable":True},
            {"name":"diag_steps","type":"Edm.String","searchable":True},
            {"name":"escalation","type":"Edm.String","searchable":True},
            {"name":"example_msgs","type":"Edm.String","searchable":True},

            # ✅ 2024-07-01 규격: dimensions + vectorSearchProfile
            {
              "name": "vector",
              "type": "Collection(Edm.Single)",
              "searchable": True,
              "dimensions": EMBEDDING_DIMS,              # e.g., 1536 for text-embedding-3-small
              "vectorSearchProfile": "vector-profile-1"  # 아래 profiles에서 참조
            }
        ],

        # ✅ 벡터 설정: algorithms + profiles (필수)
        "vectorSearch": {
            "algorithms": [
                { "name": "hnsw-1", "kind": "hnsw" }     # 기본값으로 충분
            ],
            "profiles": [
                { "name": "vector-profile-1", "algorithm": "hnsw-1" }
            ]
            # (선택) "compressions": [...]  # 필요 시 스칼라/바이너리 양자화 추가
        },

        # ✅ 시맨틱 설정: 최신 속성명 사용
        "semantic": {
            "configurations": [{
                "name": "kb-semcfg",
                "prioritizedFields": {
                    "titleField": { "fieldName": "title" },
                    "prioritizedContentFields": [
                        { "fieldName": "root_cause" },
                        { "fieldName": "initial_actions" },
                        { "fieldName": "diag_steps" }
                    ]
                    # "prioritizedKeywordsFields": [ { "fieldName": "tags" } ]  # 있다면 추가
                }
            }],
            "defaultConfiguration": "kb-semcfg"
        }
    }
    r = requests.post(url, headers=_headers(), data=json.dumps(body))
    if r.status_code >= 300:
        raise RuntimeError(f"Create index failed: {r.status_code} {r.text}")




def ensure_index():
    if index_exists():
        print(f"[OK] Index '{INDEX_NAME}' already exists.")
    else:
        print(f"[CREATE] Creating index '{INDEX_NAME}' ...")
        create_index()
        print(f"[OK] Index created.")

# ---------- Azure OpenAI embedding ----------
import json as _json
import urllib.parse as _url

def embed_texts(texts):
    """
    Azure OpenAI Embeddings REST (공식 SDK의 최신 버전 명칭이 변경될 수 있어 REST로 고정)
    """
    url = f"{AOAI_ENDPOINT}/openai/deployments/{AOAI_EMBED_DEPLOY}/embeddings?api-version={AOAI_API_VERSION}"
    headers = {
        "Content-Type": "application/json",
        "api-key": AOAI_KEY
    }
    payload = {"input": texts}
    r = requests.post(url, headers=headers, data=_json.dumps(payload))
    if r.status_code >= 300:
        raise RuntimeError(f"Embedding failed: {r.status_code} {r.text}")
    data = r.json()
    return [d["embedding"] for d in data["data"]]

def build_vector_source(doc):
    # 임베딩 품질/비용 밸런스: title + 핵심 필드 위주
    parts = [
        doc.get("title",""),
        doc.get("symptoms",""),
        doc.get("root_cause",""),
        doc.get("initial_actions","")
    ]
    return " ".join([p for p in parts if p])

# ---------- Upsert (batch) ----------
def upsert_docs(docs, batch_size=100):
    url = f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}/docs/index?api-version=2024-07-01"
    total = len(docs)
    for i in range(0, total, batch_size):
        batch = docs[i:i+batch_size]
        payload = {"value": batch}
        r = requests.post(url, headers=_headers(), data=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if r.status_code >= 300:
            raise RuntimeError(f"Upsert failed: {r.status_code} {r.text}")
        print(f"[UPSERT] {i+1}..{min(i+batch_size,total)} / {total}")

def main():
    ensure_index()

    # KB 읽기
    if not os.path.exists(KB_JSON_PATH):
        raise FileNotFoundError(f"KB JSON not found: {KB_JSON_PATH}")
    kb = json.load(open(KB_JSON_PATH, "r", encoding="utf-8"))
    print(f"[LOAD] KB docs: {len(kb)} from {KB_JSON_PATH}")

    # 벡터 생성 (배치 임베딩 권장)
    # 긴 텍스트가 아니므로 한 번에 해도 무방하지만, 안전하게 64개 단위로 끊어서 호출
    vec_texts = [build_vector_source(d) for d in kb]
    vectors = []
    chunk = 64
    for i in range(0, len(vec_texts), chunk):
        vecs = embed_texts(vec_texts[i:i+chunk])
        vectors.extend(vecs)
        print(f"[EMBED] {i+1}..{min(i+chunk,len(vec_texts))}/{len(vec_texts)}")

    # 업서트용 문서 가공
    docs = []
    for d, v in zip(kb, vectors):
        doc = {
            "@search.action": "mergeOrUpload",
            "id": d["id"],
            "title": d.get("title"),
            "operator": d.get("operator", []),
            "direction": d.get("direction"),
            "process": d.get("process"),
            "error_code": d.get("error_code"),
            "symptoms": d.get("symptoms"),
            "root_cause": d.get("root_cause"),
            "initial_actions": d.get("initial_actions"),
            "diag_steps": d.get("diag_steps"),
            "escalation": d.get("escalation"),
            "example_msgs": d.get("example_msgs"),
            "vector": v
        }
        docs.append(doc)

    upsert_docs(docs, batch_size=100)
    print("[DONE] Indexing complete.")

if __name__ == "__main__":
    main()

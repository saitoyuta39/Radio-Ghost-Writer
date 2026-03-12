import os
import re
import json
import uuid
import pickle
import tempfile
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, g, render_template
from flask_cors import CORS
import anthropic
import openai
from dotenv import load_dotenv
from supabase import create_client, Client
import jwt as pyjwt
from jwt import PyJWKClient
from scraper import fetch_sync
from google.oauth2 import service_account
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
import io

load_dotenv()

# Vercel でパスが正しく解決されるように、絶対パスを設定
base_dir = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, 
            static_folder=os.path.join(base_dir, "static"),
            template_folder=os.path.join(base_dir, "templates"))
CORS(app)

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

DEFAULT_MODEL = "claude-opus-4-6"

# Google GCP 設定
GCP_SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip('"').strip("'").strip()
SHEET_NAME = os.getenv("SHEET_NAME", "【削除禁止】週報回答データ")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip('"').strip("'").strip()
DATE_COLUMN = "A"
QUESTION_COLUMN = "G"
SCORE_COLUMN = "P"
GCP_SERVICE_ACCOUNT_JSON = os.getenv("GCP_SERVICE_ACCOUNT_JSON")

# サービス初期化用の共通認証取得
def get_gcp_credentials():
    """GCP認証情報を取得（サービスアカウントファイル、環境変数、またはローカル）"""
    creds = None
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 1. サービスアカウントファイルが直接置いてあるか確認（最も安全・確実）
    sa_file_path = os.path.join(base_dir, 'service_account.json')
    if os.path.exists(sa_file_path):
        try:
            creds = service_account.Credentials.from_service_account_file(sa_file_path, scopes=GCP_SCOPES)
            print("GCP: service_account.json ファイルを使用して認証します")
            return creds
        except Exception as e:
            print(f"GCP: service_account.json ファイルによる認証に失敗しました: {e}")

    # 2. サービスアカウント（JSON文字列）が環境変数にあるか確認
    if GCP_SERVICE_ACCOUNT_JSON:
        try:
            info = json.loads(GCP_SERVICE_ACCOUNT_JSON, strict=False)
            creds = service_account.Credentials.from_service_account_info(info, scopes=GCP_SCOPES)
            return creds
        except Exception as e:
            print(f"GCP: サービスアカウント認証の初期化に失敗しました: {e}")
    
    # 2. ローカルのトークンや認証フロー (ローカル開発用)
    token_path = os.path.join(base_dir, 'token.pickle')
    if os.path.exists(token_path):
        try:
            with open(token_path, 'rb') as token:
                creds = pickle.load(token)
        except Exception:
            creds = None
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        
        if not creds or not creds.valid:
            credentials_path = os.path.join(base_dir, 'credentials.json')
            if os.path.exists(credentials_path):
                flow = InstalledAppFlow.from_client_secrets_file(credentials_path, GCP_SCOPES)
                creds = flow.run_local_server(port=0)
                with open(token_path, 'wb') as token:
                    pickle.dump(creds, token)
            else:
                print("GCP: 認証情報が見つかりません。'GCP_SERVICE_ACCOUNT_JSON' または 'credentials.json' が必要です。")
                return None
    return creds

# Google Sheets サービスの初期化（遅延初期化）
_sheets_service = None

def get_sheets_service():
    """Google Sheets APIサービスを取得（遅延初期化）"""
    global _sheets_service
    if _sheets_service is not None:
        return _sheets_service
    
    creds = get_gcp_credentials()
    if not creds:
        return None
    
    _sheets_service = build('sheets', 'v4', credentials=creds)
    return _sheets_service

# Google Drive サービスの初期化（遅延初期化）
_drive_service = None

def get_drive_service():
    """Google Drive APIサービスを取得（遅延初期化）"""
    global _drive_service
    if _drive_service is not None:
        return _drive_service
    
    creds = get_gcp_credentials()
    if not creds:
        return None
    
    _drive_service = build('drive', 'v3', credentials=creds)
    return _drive_service

AVAILABLE_MODELS = [
    {"id": "claude-opus-4-6", "name": "Claude Opus 4.6", "desc": "最高性能 / 長文・高品質向け"},
    {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4", "desc": "高速・バランス型"},
    {"id": "claude-haiku-4-20250514", "name": "Claude Haiku 4", "desc": "最速・低コスト"},
]

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


print(f"DEBUG: Initializing JWKS Client with URL: {SUPABASE_URL}/auth/v1/.well-known/jwks.json")
_jwks_client = PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json", cache_keys=True)


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "認証が必要です"}), 401
        token = auth_header.split(" ", 1)[1]
        try:
            # Debug: Check token header
            header = pyjwt.get_unverified_header(token)
            print(f"DEBUG: Token Received. kid: {header.get('kid')}, alg: {header.get('alg')}")
            
            signing_key = _jwks_client.get_signing_key_from_jwt(token)
            payload = pyjwt.decode(
                token,
                signing_key.key,
                algorithms=["ES256"],
                audience="authenticated",
            )
            g.user_id = payload["sub"]
        except pyjwt.ExpiredSignatureError:
            print("DEBUG: Token Expired")
            return jsonify({"error": "トークンの有効期限が切れています"}), 401
        except Exception as e:
            print(f"DEBUG: Auth Error: {type(e).__name__}: {str(e)}")
            return jsonify({"error": f"認証エラー: {str(e)}"}), 401
        return f(*args, **kwargs)
    return decorated


def call_claude(system_prompt, user_prompt, *, json_mode=False, model=None):
    messages = [{"role": "user", "content": user_prompt}]

    kwargs = {
        "model": model or DEFAULT_MODEL,
        "max_tokens": 8192,
        "system": system_prompt,
        "messages": messages,
    }

    response = client.messages.create(**kwargs)
    text = response.content[0].text

    if json_mode:
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        return json.loads(text)

    return text


def build_style_analysis_prompt(references):
    ref_texts = ""
    for i, ref in enumerate(references, 1):
        ref_texts += f"\n\n=== リファレンス原稿 {i} ===\n{ref}"

    system = load_prompt("style_analysis/system.md")
    user = load_prompt("style_analysis/user.md", ref_texts=ref_texts)

    return system, user


def _format_sources(sources):
    if not sources:
        return ""
    parts = []
    for i, src in enumerate(sources, 1):
        title = src.get("title", f"資料{i}")
        text = src.get("text", "")
        if text.strip():
            parts.append(f"=== 参考資料 {i}: {title} ===\n{text[:8000]}")
    if not parts:
        return ""
    return "\n\n## 参考資料（ラジオ原稿執筆の背景知識として活用してください）\n" + "\n\n".join(parts)


def build_interview_prompt(style_guide, title, question, memo, sources=None):
    sources_text = _format_sources(sources)
    style_guide_json = json.dumps(style_guide, ensure_ascii=False, indent=2)

    system = load_prompt("interview/start_system.md")
    user = load_prompt("interview/start_user.md", 
                       style_guide_json=style_guide_json,
                       title=title,
                       question=question,
                       memo=memo,
                       sources_text=sources_text)

    return system, user


def build_followup_prompt(style_guide, title, question, memo, conversation_history, sources=None):
    history_text = ""
    for msg in conversation_history:
        role = "インタビュアー" if msg["role"] == "assistant" else "ユーザー"
        history_text += f"\n{role}: {msg['content']}\n"

    sources_text = _format_sources(sources)
    style_guide_json = json.dumps(style_guide, ensure_ascii=False, indent=2)

    system = load_prompt("interview/followup_system.md")
    user = load_prompt("interview/followup_user.md",
                       style_guide_json=style_guide_json,
                       title=title,
                       question=question,
                       memo=memo,
                       sources_text=sources_text,
                       history_text=history_text)

    return system, user


def build_article_prompt(style_guide, title, question, memo, conversation_history, sources=None):
    history_text = ""
    for msg in conversation_history:
        role = "インタビュアー" if msg["role"] == "assistant" else "ユーザー"
        history_text += f"\n{role}: {msg['content']}\n"

    sources_text = _format_sources(sources)
    style_guide_json = json.dumps(style_guide, ensure_ascii=False, indent=2)

    system = load_prompt("creation/system.md")
    user = load_prompt("creation/user.md",
                       style_guide_json=style_guide_json,
                       title=title,
                       question=question,
                       memo=memo,
                       sources_text=sources_text,
                       history_text=history_text)

    return system, user


@app.route("/")
def index():
    return render_template(
        "index.html",
        supabase_url=os.getenv("SUPABASE_URL"),
        supabase_anon_key=os.getenv("SUPABASE_ANON_KEY")
    )


@app.route("/api/models", methods=["GET"])
def list_models():
    return jsonify({"models": AVAILABLE_MODELS, "default": DEFAULT_MODEL})


@app.route("/api/analyze-style", methods=["POST"])
@require_auth
def analyze_style():
    data = request.json
    references = data.get("references", [])
    model = data.get("model")

    if not references:
        return jsonify({"error": "リファレンスラジオ原稿を1つ以上入力してください"}), 400

    try:
        system, user = build_style_analysis_prompt(references)
        style_guide = call_claude(system, user, json_mode=True, model=model)
        return jsonify({"style_guide": style_guide})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/interview/start", methods=["POST"])
@require_auth
def start_interview():
    data = request.json
    style_guide = data.get("style_guide", {})
    title = data.get("title", "")
    question = data.get("question", "")
    memo = data.get("memo", "")
    sources = data.get("sources", [])
    model = data.get("model")

    try:
        system, user = build_interview_prompt(style_guide, title, question, memo, sources)
        ai_message = call_claude(system, user, model=model)
        return jsonify({"message": ai_message})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/interview/continue", methods=["POST"])
@require_auth
def continue_interview():
    data = request.json
    style_guide = data.get("style_guide", {})
    title = data.get("title", "")
    question = data.get("question", "")
    memo = data.get("memo", "")
    conversation = data.get("conversation", [])
    sources = data.get("sources", [])
    model = data.get("model")

    try:
        system, user = build_followup_prompt(style_guide, title, question, memo, conversation, sources)
        ai_message = call_claude(system, user, model=model)
        ready = "素材が揃いました" in ai_message
        return jsonify({"message": ai_message, "ready_to_write": ready})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate-article", methods=["POST"])
@require_auth
def generate_article():
    data = request.json
    style_guide = data.get("style_guide", {})
    title = data.get("title", "")
    question = data.get("question", "")
    memo = data.get("memo", "")
    conversation = data.get("conversation", [])
    sources = data.get("sources", [])
    model = data.get("model")

    try:
        system, user = build_article_prompt(style_guide, title, question, memo, conversation, sources)
        article_html = call_claude(system, user, model=model)
        return jsonify({"article": article_html})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/contexts", methods=["GET"])
@require_auth
def list_contexts():
    data = supabase.table("contexts").select("*").eq("user_id", g.user_id).order("created_at", desc=True).execute()
    # Map DB column names to frontend expected names
    contexts = []
    for row in data.data:
        row["references"] = row.pop("reference_texts", [])
        contexts.append(row)
    return jsonify({"contexts": contexts})


@app.route("/api/contexts", methods=["POST"])
@require_auth
def create_context():
    data = request.json
    name = data.get("name", "").strip()
    references = data.get("references", [])
    style_guide = data.get("style_guide", None)

    if not name:
        return jsonify({"error": "名前を入力してください"}), 400
    if not references or not any(r.strip() for r in references):
        return jsonify({"error": "リファレンスラジオ原稿を1つ以上入力してください"}), 400

    result = supabase.table("contexts").insert({
        "user_id": g.user_id,
        "name": name,
        "reference_texts": references,
        "style_guide": style_guide,
    }).execute()

    ctx = result.data[0]
    ctx["references"] = ctx.pop("reference_texts", [])
    return jsonify({"context": ctx})


@app.route("/api/contexts/<context_id>", methods=["PUT"])
@require_auth
def update_context(context_id):
    data = request.json

    update_data = {}
    if "name" in data:
        update_data["name"] = data["name"]
    if "references" in data:
        update_data["reference_texts"] = data["references"]
    if "style_guide" in data:
        update_data["style_guide"] = data["style_guide"]

    result = supabase.table("contexts").update(update_data).eq("id", context_id).eq("user_id", g.user_id).execute()

    if not result.data:
        return jsonify({"error": "コンテキストが見つかりません"}), 404

    ctx = result.data[0]
    ctx["references"] = ctx.pop("reference_texts", [])
    return jsonify({"context": ctx})


@app.route("/api/contexts/<context_id>/reference/<int:ref_index>", methods=["PUT"])
@require_auth
def update_single_reference(context_id, ref_index):
    data = request.json
    new_text = data.get("text", "")

    result = supabase.table("contexts").select("*").eq("id", context_id).eq("user_id", g.user_id).execute()

    if not result.data:
        return jsonify({"error": "コンテキストが見つかりません"}), 404

    context = result.data[0]
    refs = context.get("reference_texts", [])
    while len(refs) <= ref_index:
        refs.append("")
    refs[ref_index] = new_text

    update_result = supabase.table("contexts").update({
        "reference_texts": refs,
        "style_guide": None,
    }).eq("id", context_id).eq("user_id", g.user_id).execute()

    ctx = update_result.data[0]
    ctx["references"] = ctx.pop("reference_texts", [])
    return jsonify({"context": ctx})


@app.route("/api/contexts/<context_id>", methods=["DELETE"])
@require_auth
def delete_context(context_id):
    supabase.table("contexts").delete().eq("id", context_id).eq("user_id", g.user_id).execute()
    return jsonify({"success": True})


@app.route("/api/fetch-url", methods=["POST"])
@require_auth
def fetch_url():
    """URLからAPI経由でテキストを取得する"""
    import time as _time
    data = request.json
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "URLを入力してください"}), 400

    t0 = _time.time()
    print(f"[fetch-url] START url={url[:80]}", flush=True)
    try:
        result = fetch_sync(url)
        elapsed = _time.time() - t0
        text = result.get("text", "")
        meta = result.get("meta", {})
        print(f"[fetch-url] OK {len(text)} chars in {elapsed:.1f}s", flush=True)

        if text and len(text) > 50:
            return jsonify({
                "text": text[:15000],
                "source": "api",
                "message": f"ラジオ原稿を取得しました（{len(text)}文字）",
                "meta": meta,
            })

        return jsonify({
            "text": "",
            "source": "api_empty",
            "message": "テキストを取得できませんでした。URLを確認してください。",
            "meta": {},
        })
    except Exception as e:
        elapsed = _time.time() - t0
        print(f"[fetch-url] ERROR in {elapsed:.1f}s: {e}", flush=True)
        return jsonify({
            "text": "",
            "source": "error",
            "message": f"取得に失敗しました: {str(e)}",
            "meta": {},
        })


def build_rewrite_interview_prompt(style_guide, original_article, user_angle):
    style_guide_json = json.dumps(style_guide, ensure_ascii=False, indent=2)
    system = load_prompt("rewrite/interview_system.md")
    user = load_prompt("rewrite/interview_user.md",
                       style_guide_json=style_guide_json,
                       original_article=original_article[:8000],
                       user_angle=user_angle)

    return system, user


def build_rewrite_followup_prompt(style_guide, original_article, user_angle, conversation_history):
    history_text = ""
    for msg in conversation_history:
        role = "インタビュアー" if msg["role"] == "assistant" else "ユーザー"
        history_text += f"\n{role}: {msg['content']}\n"

    style_guide_json = json.dumps(style_guide, ensure_ascii=False, indent=2)
    system = load_prompt("rewrite/followup_system.md")
    user = load_prompt("rewrite/followup_user.md",
                       style_guide_json=style_guide_json,
                       original_article=original_article[:8000],
                       user_angle=user_angle,
                       history_text=history_text)

    return system, user


def build_rewrite_article_prompt(style_guide, original_article, user_angle, conversation_history, sources=None):
    history_text = ""
    for msg in conversation_history:
        role = "インタビュアー" if msg["role"] == "assistant" else "ユーザー"
        history_text += f"\n{role}: {msg['content']}\n"

    sources_text = _format_sources(sources)
    style_guide_json = json.dumps(style_guide, ensure_ascii=False, indent=2)

    system = load_prompt("rewrite/article_system.md")
    user = load_prompt("rewrite/article_user.md",
                       style_guide_json=style_guide_json,
                       original_article=original_article[:8000],
                       user_angle=user_angle,
                       history_text=history_text,
                       sources_text=sources_text)

    return system, user


@app.route("/api/rewrite/start", methods=["POST"])
@require_auth
def rewrite_start():
    data = request.json
    style_guide = data.get("style_guide", {})
    original_article = data.get("original_article", "")
    user_angle = data.get("user_angle", "")
    model = data.get("model")

    try:
        system, user = build_rewrite_interview_prompt(style_guide, original_article, user_angle)
        ai_message = call_claude(system, user, model=model)
        return jsonify({"message": ai_message})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rewrite/continue", methods=["POST"])
@require_auth
def rewrite_continue():
    data = request.json
    style_guide = data.get("style_guide", {})
    original_article = data.get("original_article", "")
    user_angle = data.get("user_angle", "")
    conversation = data.get("conversation", [])
    model = data.get("model")

    try:
        system, user = build_rewrite_followup_prompt(style_guide, original_article, user_angle, conversation)
        ai_message = call_claude(system, user, model=model)
        ready = "素材が揃いました" in ai_message
        return jsonify({"message": ai_message, "ready_to_write": ready})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rewrite/generate", methods=["POST"])
@require_auth
def rewrite_generate():
    data = request.json
    style_guide = data.get("style_guide", {})
    original_article = data.get("original_article", "")
    user_angle = data.get("user_angle", "")
    conversation = data.get("conversation", [])
    sources = data.get("sources", [])
    model = data.get("model")

    try:
        system, user = build_rewrite_article_prompt(style_guide, original_article, user_angle, conversation, sources)
        article_html = call_claude(system, user, model=model)
        return jsonify({"article": article_html})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/articles/<article_id>/export-drive", methods=["POST"])
@require_auth
def export_article_to_drive(article_id):
    """ラジオ原稿をGoogle DriveにMDファイルとしてエクスポートする"""
    if not GOOGLE_DRIVE_FOLDER_ID:
        return jsonify({"error": "Google DriveのフォルダIDが設定されていません。"}), 400
    
    drive_service = get_drive_service()
    if not drive_service:
        return jsonify({"error": "Google Drive APIの認証に失敗しました。"}), 500
    
    # 1. Supabaseから記事内容を取得
    result = supabase.table("articles").select("*").eq("id", article_id).eq("user_id", g.user_id).execute()
    if not result.data:
        return jsonify({"error": "ラジオ原稿が見つかりません。"}), 404
    
    article = result.data[0]
    title = article.get("title", "無題")
    html_content = article.get("html", "")
    text_content = article.get("text_content", "")
    
    # ファイル名 (MD形式)
    filename = f"{title}.md"
    
    # MDファイルの内容を作成
    # Markdownソース（htmlフィールド）を優先的に使用
    md_content = html_content if html_content else text_content
    
    try:
        # メモリ上のストリームとしてファイル作成
        file_metadata = {
            'name': filename,
            'parents': [GOOGLE_DRIVE_FOLDER_ID]
        }
        
        media = MediaIoBaseUpload(
            io.BytesIO(md_content.encode('utf-8')),
            mimetype='text/markdown',
            resumable=True
        )
        
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id',
            supportsAllDrives=True  # 共有ドライブをサポート
        ).execute()
        
        return jsonify({"success": True, "file_id": file.get('id')})
    
    except HttpError as error:
        error_details = error.reason if hasattr(error, 'reason') else str(error)
        print(f"Drive Export HttpError: {error_details}")
        return jsonify({"error": f"Google Drive APIエラー: {error_details}"}), 500
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"エクスポート中にエラーが発生しました: {str(e)}"}), 500


@app.route("/api/extract-source", methods=["POST"])
@require_auth
def extract_source():
    """URLまたはPDFからテキストを抽出する汎用エンドポイント"""
    url = (request.form.get("url") or "").strip()
    pdf_file = request.files.get("file")

    if pdf_file and pdf_file.filename:
        return _extract_pdf(pdf_file)
    elif url:
        return _extract_url(url)
    else:
        return jsonify({"error": "URLまたはPDFファイルを指定してください"}), 400


def _extract_pdf(pdf_file):
    try:
        import fitz
    except ImportError:
        return jsonify({"error": "PyMuPDF が未インストールです"}), 500

    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            pdf_file.save(tmp.name)
            tmp_path = tmp.name

        doc = fitz.open(tmp_path)
        pages = []
        for page in doc:
            pages.append(page.get_text())
        doc.close()
        os.unlink(tmp_path)

        text = "\n\n".join(pages).strip()
        if not text:
            return jsonify({"text": "", "title": pdf_file.filename, "message": "PDFからテキストを抽出できませんでした。"})

        return jsonify({
            "text": text[:30000],
            "title": pdf_file.filename,
            "message": f"PDFから抽出しました（{len(text)}文字）",
        })
    except Exception as e:
        return jsonify({"error": f"PDF抽出エラー: {str(e)}"}), 500


def _extract_url(url):
    try:
        result = fetch_sync(url)
        text = result.get("text", "")
        meta = result.get("meta", {})
        title = meta.get("article_title", "") or url
        return jsonify({
            "text": text[:30000],
            "title": title,
            "message": f"取得しました（{len(text)}文字）",
        })
    except Exception as e:
        return jsonify({"error": f"取得に失敗しました: {str(e)}"}), 500


@app.route("/api/articles", methods=["GET"])
@require_auth
def list_articles():
    data = supabase.table("articles").select("*").eq("user_id", g.user_id).order("created_at", desc=True).execute()
    # Map DB column name text_content -> text for frontend compatibility
    articles = []
    for row in data.data:
        row["text"] = row.pop("text_content", "")
        articles.append(row)
    return jsonify({"articles": articles})


@app.route("/api/articles", methods=["POST"])
@require_auth
def create_article():
    data = request.json
    title = data.get("title", "").strip()
    question = data.get("question", "")
    html = data.get("html", "")
    text = data.get("text", "")
    memo = data.get("memo", "")
    conversation = data.get("conversation", [])
    context_id = data.get("context_id", None)

    if not title:
        return jsonify({"error": "タイトルを入力してください"}), 400

    result = supabase.table("articles").insert({
        "user_id": g.user_id,
        "title": title,
        "question": question,
        "html": html,
        "text_content": text,
        "memo": memo,
        "conversation": conversation,
        "context_id": context_id,
    }).execute()

    art = result.data[0]
    art["text"] = art.pop("text_content", "")
    return jsonify({"article": art})


@app.route("/api/articles/<article_id>", methods=["PUT"])
@require_auth
def update_article(article_id):
    data = request.json

    update_data = {}

    if "html" in data:
        update_data["html"] = data["html"]

    if "text" in data:
        update_data["text_content"] = data["text"]

    if "title" in data:
        update_data["title"] = data["title"]

    if "question" in data:
        update_data["question"] = data["question"]

    if "memo" in data:
        update_data["memo"] = data["memo"]

    if "conversation" in data:
        update_data["conversation"] = data["conversation"]

    if "context_id" in data:
        update_data["context_id"] = data["context_id"]

    if "status" in data:
        if data["status"] not in ["draft", "interviewing", "completed"]:
            return jsonify({"error": "無効なステータスです"}), 400
        update_data["status"] = data["status"]

    if not update_data:
        return jsonify({"error": "更新するデータがありません"}), 400

    result = supabase.table("articles").update(update_data).eq("id", article_id).eq("user_id", g.user_id).execute()

    if not result.data:
        return jsonify({"error": "ラジオ原稿が見つかりません"}), 404

    art = result.data[0]
    art["text"] = art.pop("text_content", "")
    return jsonify({"article": art})


@app.route("/api/articles/<article_id>", methods=["DELETE"])
@require_auth
def delete_article(article_id):
    supabase.table("articles").delete().eq("id", article_id).eq("user_id", g.user_id).execute()
    return jsonify({"success": True})


@app.route("/api/article/edit-selection", methods=["POST"])
@require_auth
def edit_selection():
    data = request.json
    full_html = data.get("full_html", "")
    selected_text = data.get("selected_text", "")
    instruction = data.get("instruction", "")
    style_guide = data.get("style_guide", {})
    model = data.get("model")

    style_guide_text = f"## スタイルガイド\n{json.dumps(style_guide, ensure_ascii=False)}\n\n" if style_guide else ""

    system = load_prompt("edit/selection_system.md")
    user = load_prompt("edit/selection_user.md",
                       style_guide_text=style_guide_text,
                       full_html=full_html,
                       selected_text=selected_text,
                       instruction=instruction)

    try:
        result = call_claude(system, user, model=model)
        return jsonify({"article": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/article/edit-full", methods=["POST"])
@require_auth
def edit_full():
    data = request.json
    full_html = data.get("full_html", "")
    instruction = data.get("instruction", "")
    style_guide = data.get("style_guide", {})
    model = data.get("model")

    style_guide_text = f"## スタイルガイド\n{json.dumps(style_guide, ensure_ascii=False)}\n\n" if style_guide else ""

    system = load_prompt("edit/full_system.md")
    user = load_prompt("edit/full_user.md",
                       style_guide_text=style_guide_text,
                       full_html=full_html,
                       instruction=instruction)

    try:
        result = call_claude(system, user, model=model)
        return jsonify({"article": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


PROMPTS_DIR = os.path.join(base_dir, "prompts")
PROMPTS_DEFAULT_DIR = os.path.join(base_dir, "prompts_default")

PROMPT_GROUP_NAMES = {
    "creation": "新規作成",
    "interview": "インタビュー",
    "edit": "編集・修正",
    "rewrite": "リライト",
    "selection": "選定・採点",
    "style_analysis": "スタイル分析",
}

PROMPT_FILE_TITLES = {
    "creation/system.md": "システム設定",
    "creation/user.md": "ユーザー指示",
    "selection/scoring_system.md": "採点システム",
    "interview/start_system.md": "初回システム",
    "interview/start_user.md": "初回ユーザー",
    "interview/followup_system.md": "追記システム",
    "interview/followup_user.md": "追記ユーザー",
    "edit/full_system.md": "全体編集システム",
    "edit/full_user.md": "全体編集ユーザー",
    "edit/selection_system.md": "選択編集システム",
    "edit/selection_user.md": "選択編集ユーザー",
    "rewrite/article_system.md": "記事リライトシステム",
    "rewrite/article_user.md": "記事リライトユーザー",
    "rewrite/followup_system.md": "追記リライトシステム",
    "rewrite/followup_user.md": "追記リライトユーザー",
    "rewrite/interview_system.md": "リライトインタビューシステム",
    "rewrite/interview_user.md": "リライトインタビューユーザー",
    "style_analysis/system.md": "スタイル分析システム",
    "style_analysis/user.md": "スタイル分析ユーザー",
}

def extract_prompt_title(path):
    """ファイルパスから日本語タイトルを返す（定義にない場合はファイル名を返す）"""
    return PROMPT_FILE_TITLES.get(path, os.path.basename(path))

def get_prompt_content_from_db_or_file(path):
    """DB（Supabase）からカスタムプロンプトを取得し、なければ prompts/ フォルダから取得する"""
    normalized_path = path.replace("\\", "/")
    
    try:
        # 1. Supabase から取得を試みる
        result = supabase.table("custom_prompts").select("content").eq("path", normalized_path).execute()
        if result.data:
            return result.data[0]["content"]
    except Exception as e:
        print(f"DEBUG: Failed to fetch prompt from DB ({normalized_path}): {e}")

    # 2. DBにない場合は prompts/ フォルダから取得
    full_path = os.path.join(PROMPTS_DIR, path)
    if not os.path.exists(full_path):
        raise FileNotFoundError(f"Prompt file not found: {full_path}")
    
    with open(full_path, "r", encoding="utf-8") as f:
        return f.read()

def load_prompt(path, **kwargs):
    """プロンプトを読み込み、変数を展開する"""
    content = get_prompt_content_from_db_or_file(path)
    
    if not kwargs:
        return content
    
    result = content
    for key, value in kwargs.items():
        placeholder = "{" + key + "}"
        result = result.replace(placeholder, str(value))
    
    return result

@app.route("/api/prompts", methods=["GET"])
@require_auth
def list_prompts():
    """プロンプトファイルの一覧をグループ化して取得する"""
    # ... (既存の walk ロジック) ...
    if not os.path.exists(PROMPTS_DIR):
        return jsonify({"groups": []})
        
    groups_dict = {}
    
    for root, dirs, files in os.walk(PROMPTS_DIR):
        for file in files:
            if file.endswith(".md"):
                rel_path = os.path.relpath(os.path.join(root, file), PROMPTS_DIR)
                normalized_path = rel_path.replace("\\", "/")
                
                # フォルダ名（最上位）をグループIDとする
                parts = normalized_path.split("/")
                group_id = parts[0] if len(parts) > 1 else "other"
                
                if group_id not in groups_dict:
                    groups_dict[group_id] = {
                        "id": group_id,
                        "name": PROMPT_GROUP_NAMES.get(group_id, group_id),
                        "prompts": []
                    }
                
                groups_dict[group_id]["prompts"].append({
                    "path": normalized_path,
                    "title": extract_prompt_title(normalized_path)
                })
    
    # グループID順にソート（定義順、なければID順）
    order = list(PROMPT_GROUP_NAMES.keys())
    sorted_groups = []
    for g_id in order:
        if g_id in groups_dict:
            sorted_groups.append(groups_dict.pop(g_id))
    
    # 残りのグループを追加
    for g_id in sorted(groups_dict.keys()):
        sorted_groups.append(groups_dict[g_id])
        
    return jsonify({"groups": sorted_groups})

@app.route("/api/prompts/<path:filename>", methods=["GET"])
@require_auth
def get_prompt(filename):
    """プロンプトの内容を取得する（DB優先）"""
    try:
        content = get_prompt_content_from_db_or_file(filename)
        return jsonify({"content": content})
    except FileNotFoundError:
        return jsonify({"error": "ファイルが見つかりません"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/prompts/<path:filename>", methods=["PUT"])
@require_auth
def update_prompt(filename):
    """プロンプトの内容を更新する（DBに保存）"""
    data = request.json
    content = data.get("content")
    if content is None:
        return jsonify({"error": "内容がありません"}), 400
    
    normalized_path = filename.replace("\\", "/")
    
    try:
        # Supabase に保存（upsert）
        supabase.table("custom_prompts").upsert({
            "path": normalized_path,
            "content": content,
            "updated_at": "now()"
        }).execute()
        
        # ローカル環境（writableな環境）ならファイルも更新しておく（任意）
        safe_path = os.path.normpath(os.path.join(PROMPTS_DIR, filename))
        if os.path.abspath(safe_path).startswith(os.path.abspath(PROMPTS_DIR)):
            try:
                os.makedirs(os.path.dirname(safe_path), exist_ok=True)
                with open(safe_path, "w", encoding="utf-8", newline="") as f:
                    f.write(content)
            except Exception:
                # Vercel などの読み取り専用環境では無視して良い
                pass
                
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/prompts/reset/<path:filename>", methods=["POST"])
@require_auth
def reset_prompt(filename):
    """プロンプトをデプロイ時の状態（prompts/ フォルダの内容）にリセットする"""
    normalized_path = filename.replace("\\", "/")
    
    try:
        # 1. Supabase からカスタム設定を削除
        supabase.table("custom_prompts").delete().eq("path", normalized_path).execute()
        
        # 2. デプロイされている prompts/ フォルダ内のファイル内容を取得して返す
        source_path = os.path.normpath(os.path.join(PROMPTS_DIR, filename))
        if not os.path.exists(source_path):
            return jsonify({"error": "原本ファイルが見つかりません"}), 404
            
        with open(source_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        return jsonify({"success": True, "content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== 質問管理 API ====================

def col_to_idx(col_str):
    """列文字をインデックスに変換（A=0, B=1, ...）"""
    return ord(col_str.upper()) - ord('A')


@app.route("/api/questions", methods=["GET"])
@require_auth
def get_questions():
    """スプレッドシートから質問を取得する"""
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")
    min_score = request.args.get("min_score", type=int, default=0)
    
    # 日付のパース
    start_date = None
    end_date = None
    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        except ValueError:
            pass
    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        except ValueError:
            pass
    
    sheets_service = get_sheets_service()
    if not sheets_service:
        return jsonify({"error": "Google Sheets APIの認証に失敗しました。環境変数を確認してください。"}), 500
    
    # 列範囲を計算
    cols = [DATE_COLUMN, QUESTION_COLUMN, SCORE_COLUMN]
    min_col_idx = min(col_to_idx(c) for c in cols)
    max_col_idx = max(col_to_idx(c) for c in cols)
    min_col_str = chr(ord('A') + min_col_idx)
    max_col_str = chr(ord('A') + max_col_idx)
    range_name = f'{SHEET_NAME}!{min_col_str}:{max_col_str}'
    
    try:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=range_name
        ).execute()
        
        values = result.get('values', [])
        if not values:
            return jsonify({"questions": []})
        
        # 各列の相対インデックス
        date_idx = col_to_idx(DATE_COLUMN) - min_col_idx
        question_idx = col_to_idx(QUESTION_COLUMN) - min_col_idx
        score_idx = col_to_idx(SCORE_COLUMN) - min_col_idx
        
        questions = []
        for i, row in enumerate(values):
            # ヘッダー行をスキップ
            if i == 0:
                continue
            
            def get_val(idx):
                if len(row) > idx and row[idx] is not None and str(row[idx]).strip() != "":
                    return str(row[idx]).strip()
                return ""
            
            date_str = get_val(date_idx)
            question_text = get_val(question_idx)
            score_str = get_val(score_idx)
            
            # 受信日がない行はスキップ（無効な行とみなす）
            if not date_str:
                continue
            
            # スコアをパース
            score = None
            if score_str:
                try:
                    # 整数または浮動小数点形式の文字列を整数に変換
                    score = int(float(score_str))
                except ValueError:
                    pass
            
            # 日付フィルタリング
            if start_date and end_date:
                try:
                    q_date = None
                    # 柔軟な日付解析
                    for fmt in ('%Y/%m/%d %H:%M:%S', '%Y/%m/%d %H:%M', '%Y/%m/%d', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
                        try:
                            # 日付部分のみ抽出して比較
                            date_part = date_str.split()[0] if ' ' in date_str else date_str
                            q_date = datetime.strptime(date_part, fmt.split()[0]).date()
                            break
                        except ValueError:
                            continue
                    
                    if q_date:
                        if not (start_date <= q_date <= end_date):
                            continue
                    else:
                        continue
                except Exception:
                    continue
            
            # スコアフィルタリング
            if min_score > 0 and (score is None or score < min_score):
                continue
            
            questions.append({
                'row': i + 1,
                'question': question_text,
                'date': date_str,
                'score': score
            })
        
        return jsonify({"questions": questions})
    
    except HttpError as error:
        error_details = error.reason if hasattr(error, 'reason') else str(error)
        return jsonify({"error": f"Google Sheets APIエラー: {error_details}"}), 500
    except Exception as e:
        return jsonify({"error": f"質問取得中にエラーが発生しました: {str(e)}"}), 500


@app.route("/api/questions/score", methods=["POST"])
@require_auth
def score_questions():
    """o3-miniで質問を一括採点する"""
    data = request.json
    questions = data.get("questions", [])
    
    if not questions:
        return jsonify({"error": "採点する質問がありません"}), 400
    
    # 採点プロンプトを読み込む
    system_prompt = load_prompt("selection/scoring_system.md")
    
    # 質問リストを作成
    questions_text = ""
    for idx, q in enumerate(questions, 1):
        questions_text += f"{idx}.\n{q['question']}\n\n"
    
    user_prompt = f"【採点対象の質問一覧】\n\n{questions_text}"
    
    try:
        response = openai_client.chat.completions.create(
            model="o3-mini",
            messages=[
                {"role": "developer", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"}
        )
        
        result_text = response.choices[0].message.content
        
        # JSONをパース
        try:
            result_data = json.loads(result_text)
            print(f"AI Score Result: {result_text}")
            
            scores = []
            # 1. 配列形式
            if isinstance(result_data, list):
                scores = result_data
            # 2. オブジェクト内の配列を探す ("scores" などのキー)
            elif isinstance(result_data, dict):
                found_list = False
                for key in result_data:
                    if isinstance(result_data[key], list):
                        scores = result_data[key]
                        found_list = True
                        break
                
                # 3. 1件のみの場合などで、直接オブジェクトが入っている
                if not found_list and "score" in result_data:
                    # indexがなければ1とする
                    if "index" not in result_data:
                        result_data["index"] = 1
                    scores = [result_data]
            else:
                scores = []
        except json.JSONDecodeError:
            print(f"JSON Decode Error. Raw text: {result_text}")
            return jsonify({"error": "AIからの応答をパースできませんでした"}), 500
        
        # スコアを質問に割り当て
        scored_results = []
        for score_item in scores:
            try:
                idx_val = score_item.get("index")
                if idx_val is None:
                    continue
                idx = int(idx_val) - 1
                if 0 <= idx < len(questions):
                    scored_results.append({
                        "row": questions[idx]["row"],
                        "score": int(score_item.get("score", 0))
                    })
            except (ValueError, TypeError):
                continue
        
        return jsonify({"scored": scored_results})
    
    except Exception as e:
        return jsonify({"error": f"採点中にエラーが発生しました: {str(e)}"}), 500


@app.route("/api/questions/update-score", methods=["POST"])
@require_auth
def update_question_scores():
    """スプレッドシートのスコア列を更新する"""
    data = request.json
    updates = data.get("updates", [])
    
    if not updates:
        return jsonify({"error": "更新するデータがありません"}), 400
    
    sheets_service = get_sheets_service()
    if not sheets_service:
        return jsonify({"error": "Google Sheets APIの認証に失敗しました"}), 500
    
    try:
        # バッチ更新用のデータを構築
        batch_data = []
        for update in updates:
            row = update.get("row")
            score = update.get("score")
            if row and score is not None:
                batch_data.append({
                    "range": f"{SHEET_NAME}!{SCORE_COLUMN}{row}",
                    "values": [[str(score)]]
                })
        
        if batch_data:
            sheets_service.spreadsheets().values().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={
                    "valueInputOption": "RAW",
                    "data": batch_data
                }
            ).execute()
        
        return jsonify({"success": True, "updated": len(batch_data)})
    
    except HttpError as error:
        error_details = error.reason if hasattr(error, 'reason') else str(error)
        return jsonify({"error": f"Google Sheets APIエラー: {error_details}"}), 500
    except Exception as e:
        return jsonify({"error": f"スコア更新中にエラーが発生しました: {str(e)}"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5050)

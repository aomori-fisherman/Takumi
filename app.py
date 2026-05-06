"""
SAGE — AI食事PFC分析
v2.2

Gemini Vision API + Streamlit
食事写真からAIが料理を識別しPFC・カロリーを自動算出
コンセプト: 賢く食べろ

v2.0: Basic機能追加
- プロフィール設定（TDEE・目標PFC自動計算）
- 今日の残り枠表示
- 食事履歴のlocalStorage保存
- 食事履歴リスト（直近7日）

v2.1: Pro機能追加
- 大会・目標管理（減量/増量モード）
- 大会カウントダウン・推奨ペース表示
- Bloom（AIポージング分析）リンク

v2.2: Gym機能追加
- トレーニングログ（部位・種目・セット記録）
- カスタム種目追加・localStorage保存
- 過去の記録を日付別に閲覧
"""

import streamlit as st
import google.generativeai as genai
from PIL import Image, ImageOps
from dotenv import load_dotenv
import os
import json
import re
from datetime import datetime, timedelta, timezone

load_dotenv()

# ============================================================
# Supabase連携（オプション）
# ============================================================
try:
    from supabase import create_client, Client
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False


@st.cache_resource
def get_supabase() -> "Client | None":
    """Supabaseクライアントを初期化（キャッシュ）"""
    if not SUPABASE_AVAILABLE:
        return None
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_ANON_KEY")
    if url and key:
        return create_client(url, key)
    return None


def db_load(supabase: "Client", user_id: str) -> "dict | None":
    """Load user data from Supabase"""
    try:
        result = supabase.table("user_data").select("*").eq("id", user_id).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception:
        return None


def db_save(supabase: "Client", user_id: str, key: str, value):
    """Save a single field to Supabase (upsert)"""
    try:
        supabase.table("user_data").upsert({
            "id": user_id,
            key: value,
            "updated_at": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        }).execute()
    except Exception:
        pass

# 日本時間（UTC+9）
JST = timezone(timedelta(hours=9))

def now_jst():
    """日本時間の現在日時を返す"""
    return datetime.now(JST)

# ============================================================
# レートリミット設定（1セッションあたり）
# ============================================================
RATE_LIMIT_MAX = 15  # 1時間あたりの最大分析回数
RATE_LIMIT_WINDOW = timedelta(hours=1)

# ============================================================
# ページ設定
# ============================================================
try:
    from PIL import Image as _PILImage
    import os as _os
    _icon_path = _os.path.join(_os.path.dirname(__file__), "sage-icon-180.png")
    _page_icon = _PILImage.open(_icon_path) if _os.path.exists(_icon_path) else "🌿"
except Exception:
    _page_icon = "🌿"

st.set_page_config(
    page_title="Sage",
    page_icon=_page_icon,
    layout="centered",
    initial_sidebar_state="collapsed",
)

# Apple touch icon for home screen (inject into parent <head> via JS)
import streamlit.components.v1 as _components
_components.html("""
<script>
(function(){
  try {
    var doc = window.parent.document;
    var iconUrl = 'https://aomori-fisherman.github.io/genkai-ryoshi/sage-icon-180.png';
    if (!doc.querySelector('link[rel="apple-touch-icon"]')) {
      var link = doc.createElement('link');
      link.rel = 'apple-touch-icon';
      link.sizes = '180x180';
      link.href = iconUrl;
      doc.head.appendChild(link);
    }
    if (!doc.querySelector('link[rel="icon"][sizes="192x192"]')) {
      var link2 = doc.createElement('link');
      link2.rel = 'icon';
      link2.type = 'image/png';
      link2.sizes = '192x192';
      link2.href = iconUrl;
      doc.head.appendChild(link2);
    }
  } catch(e) {}
})();
</script>
""", height=0)

# ============================================================
# localStorage連携（streamlit-js-eval）
# ============================================================
try:
    from streamlit_js_eval import streamlit_js_eval
    JS_EVAL_AVAILABLE = True
except ImportError:
    JS_EVAL_AVAILABLE = False


def ls_get_all(keys: list):
    """localStorageから複数キーを一括取得。pending writesがあれば先に書き込む"""
    if not JS_EVAL_AVAILABLE:
        return {k: None for k in keys}
    try:
        # pending writesがあれば書き込みJSを先に実行
        pending = st.session_state.get("_ls_pending_writes", {})
        write_js = ""
        if pending:
            for pk, pv in pending.items():
                json_str = json.dumps(pv, ensure_ascii=False)
                escaped = json.dumps(json_str)
                write_js += f"localStorage.setItem('{pk}', {escaped});"
            # 書き込み完了後にキューをクリア
            st.session_state["_ls_pending_writes"] = {}
            st.session_state["_ls_write_status"] = f"✅ {len(pending)}件書込済"

        js_code = write_js + "JSON.stringify({" + ",".join(
            f'"{k}": localStorage.getItem("{k}")' for k in keys
        ) + "})"
        raw = streamlit_js_eval(js_expressions=js_code)
        if raw is None:
            # JS実行自体が失敗 → pending writesを復元
            if pending:
                st.session_state["_ls_pending_writes"] = pending
                st.session_state["_ls_write_status"] = "⏳ 書込待機中（次回実行）"
            return {k: None for k in keys}
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        result = {}
        for k in keys:
            v = parsed.get(k)
            if v is not None:
                try:
                    result[k] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    result[k] = None
            else:
                result[k] = None
        st.session_state["_ls_load_success"] = True
        return result
    except Exception:
        return {k: None for k in keys}


def ls_get(key: str):
    """localStorageから値を取得。失敗時はNone"""
    result = ls_get_all([key])
    return result.get(key)


def ls_set(key: str, value):
    """localStorageへの書き込みをキューに入れる + Supabase同期"""
    if not st.session_state.get("_ls_load_success", False):
        st.session_state["_ls_write_status"] = "❌ 初回読込未完了"
        return False
    if "_ls_pending_writes" not in st.session_state:
        st.session_state["_ls_pending_writes"] = {}
    st.session_state["_ls_pending_writes"][key] = value
    st.session_state["_ls_write_status"] = "⏳ 書込キュー追加"

    # Supabase sync (if logged in)
    session = st.session_state.get("supabase_session")
    if session:
        try:
            sb = get_supabase()
            if sb:
                col_map = {
                    "sage_profile": "profile",
                    "sage_meals": "meals",
                    "sage_training_logs": "training_logs",
                    "sage_cardio_logs": "cardio_logs",
                    "sage_training_sessions": "training_sessions",
                    "sage_weight_log": "weight_log",
                    "sage_custom_exercises": "custom_exercises",
                }
                col = col_map.get(key)
                if col:
                    db_save(sb, session.user.id, col, value)
        except Exception:
            pass  # Silent fail — localStorage is primary

    return True


# ============================================================
# アクセスコード（プラン認証）
# ============================================================
STANDARD_CODE = os.environ.get("STANDARD_CODE", "")
TRAINEE_CODE = os.environ.get("TRAINEE_CODE", "")
STAGE_CODE = os.environ.get("STAGE_CODE", "")

# ============================================================
# トレーニング種目データベース
# ============================================================
EXERCISE_DB = {
    "胸": [
        "ベンチプレス", "インクラインベンチプレス", "デクラインベンチプレス",
        "ダンベルベンチプレス", "ダンベルインクラインプレス",
        "チェストプレス（マシン）", "シーテッドインクラインプレス", "スミスインクラインプレス", "ディップス",
        "ダンベルフライ", "インクラインダンベルフライ", "ケーブルフライ",
        "マシンフライ", "マシンインクラインフライ", "ペックフライ",
        "プルオーバー",
    ],
    "背中": [
        "デッドリフト", "ベントオーバーロウ", "ダンベルロウ", "Tバーロウ",
        "ラットプルダウン", "チンニング（懸垂）", "ワイドグリップラットプルダウン",
        "アンダーグリップラットプルダウン",
        "シーテッドケーブルロウ", "マシンロウ", "ワンアームケーブルロウ",
        "フェイスプル", "シュラッグ", "ハイパーエクステンション",
    ],
    "肩": [
        "オーバーヘッドプレス", "ダンベルショルダープレス", "マシンショルダープレス",
        "アーノルドプレス",
        "サイドレイズ", "フロントレイズ", "リアレイズ", "ケーブルサイドレイズ",
        "インクラインサイドレイズ",
        "アップライトロウ", "フェイスプル",
    ],
    "腕": [
        "バーベルカール", "ダンベルカール", "ハンマーカール", "インクラインカール",
        "プリーチャーカール", "ケーブルカール", "マシンカール", "コンセントレーションカール",
        "フレンチプレス", "インクラインフレンチプレス", "スカルクラッシャー",
        "トライセプスプレスダウン", "ディップス（三頭）",
        "オーバーヘッドトライセプスエクステンション", "キックバック",
        "リストカール", "リバースリストカール",
    ],
    "脚": [
        "スクワット", "フロントスクワット", "ブルガリアンスクワット",
        "ゴブレットスクワット", "ハックスクワット",
        "レッグプレス", "レッグエクステンション", "レッグカール",
        "ルーマニアンデッドリフト",
        "ヒップスラスト", "カーフレイズ", "レッグアダクション",
        "レッグアブダクション", "シシースクワット",
    ],
    "腹": [
        "アブローラー", "ハンギングレッグレイズ", "ケーブルクランチ",
        "シットアップ", "レッグレイズ", "プランク", "サイドベント",
    ],
}

# ============================================================
# 活動レベル定義
# ============================================================
ACTIVITY_LEVELS = {
    "軽い（デスクワーク中心）": 1.5,
    "普通（立ち仕事・軽い運動）": 1.7,
    "高い（肉体労働・毎日運動）": 1.9,
    "非常に高い（漁師・農業＋ジム）": 2.1,
}

# ============================================================
# カスタムCSS
# ============================================================
st.markdown("""
<style>
    /* ヘッダー */
    .sage-header {
        text-align: center;
        padding: 1rem 0 0.5rem 0;
        position: relative;
    }
    .sage-reload {
        position: absolute;
        top: 1rem;
        right: 0;
        background: none;
        border: 1px solid #ddd;
        border-radius: 8px;
        padding: 4px 10px;
        font-size: 1.2rem;
        cursor: pointer;
        color: #7f8c8d;
    }
    .sage-reload:hover {
        background: #f0f0f0;
    }
    .sage-header h1 {
        color: #2ECC71;
        font-size: 2.2rem;
        margin-bottom: 0;
    }
    .sage-header p {
        color: #7f8c8d;
        font-size: 0.95rem;
        margin-top: 0.2rem;
    }

    /* 合計カード */
    .total-card {
        background: linear-gradient(135deg, #2ECC71 0%, #27AE60 100%);
        color: white;
        border-radius: 12px;
        padding: 1.2rem;
        margin: 1rem 0;
        text-align: center;
    }
    .total-card h2 {
        margin: 0 0 0.5rem 0;
        font-size: 1.1rem;
        font-weight: 600;
    }
    .total-card .cal-big {
        font-size: 2.4rem;
        font-weight: 700;
        line-height: 1;
    }
    .total-card .cal-unit {
        font-size: 0.9rem;
        opacity: 0.85;
    }

    /* 残り枠カード */
    .remaining-card {
        background: #ffffff;
        border: 2px solid #2ECC71;
        border-radius: 12px;
        padding: 1.2rem;
        margin: 0 0 1rem 0;
        text-align: center;
    }
    .remaining-card h2 {
        margin: 0 0 0.5rem 0;
        font-size: 1.1rem;
        font-weight: 600;
        color: #2c3e50;
    }
    .remaining-card .remaining-cal {
        font-size: 2rem;
        font-weight: 700;
        line-height: 1;
        color: #27AE60;
    }
    .remaining-card .remaining-cal.over {
        color: #e74c3c;
    }
    .remaining-card .remaining-unit {
        font-size: 0.85rem;
        color: #7f8c8d;
    }
    .remaining-card .remaining-pfc {
        margin-top: 0.5rem;
        font-size: 0.9rem;
        color: #555;
    }

    /* PFCバー */
    .pfc-row {
        display: flex;
        gap: 0.5rem;
        margin: 0.8rem 0;
    }
    .pfc-item {
        flex: 1;
        text-align: center;
        background: #f0faf4;
        border-radius: 8px;
        padding: 0.6rem 0.3rem;
    }
    .pfc-item .label {
        font-size: 0.75rem;
        color: #7f8c8d;
    }
    .pfc-item .value {
        font-size: 1.3rem;
        font-weight: 700;
        color: #2c3e50;
    }
    .pfc-item .unit {
        font-size: 0.7rem;
        color: #95a5a6;
    }
    .pfc-p { border-left: 3px solid #3498db; }
    .pfc-f { border-left: 3px solid #e67e22; }
    .pfc-c { border-left: 3px solid #f1c40f; }

    /* 料理カード（折りたたみヘッダー） */
    .dish-header {
        background: #f9f9f9;
        border-radius: 10px;
        padding: 0.8rem 1rem;
        margin: 0.6rem 0 0 0;
        border-left: 4px solid #2ECC71;
    }
    .dish-header .dish-name {
        font-size: 1.05rem;
        font-weight: 700;
        color: #2c3e50;
        margin: 0;
    }
    .dish-header .dish-summary {
        display: flex;
        gap: 0.8rem;
        margin-top: 0.3rem;
        font-size: 0.8rem;
    }
    .dish-header .dish-summary .ds-cal {
        color: #27AE60;
        font-weight: 600;
    }
    .dish-header .dish-summary .ds-p {
        color: #3498db;
        font-weight: 600;
    }
    .dish-header .dish-summary .ds-f {
        color: #e67e22;
        font-weight: 600;
    }
    .dish-header .dish-summary .ds-c {
        color: #f1c40f;
        font-weight: 600;
    }

    /* 食材行 */
    .ing-row {
        display: flex;
        align-items: center;
        padding: 0.4rem 0;
        border-bottom: 1px solid #f0f0f0;
    }
    .ing-row .ing-name {
        flex: 2;
        font-size: 0.95rem;
        font-weight: 600;
        color: #2c3e50;
    }
    .ing-row .ing-vals {
        flex: 3;
        display: flex;
        gap: 0.3rem;
        font-size: 0.75rem;
    }
    .ing-val {
        flex: 1;
        text-align: center;
        border-radius: 4px;
        padding: 0.2rem 0;
    }
    .ing-val-cal { background: #e8f8f0; color: #27AE60; }
    .ing-val-p { background: #ebf5fb; color: #3498db; }
    .ing-val-f { background: #fef5e7; color: #e67e22; }
    .ing-val-c { background: #fef9e7; color: #f1c40f; }

    /* フッター */
    .sage-footer {
        text-align: center;
        padding: 2rem 0 1rem 0;
        color: #bdc3c7;
        font-size: 0.8rem;
    }
    .sage-footer a {
        color: #2ECC71;
        text-decoration: none;
    }

    /* サイドバー プロフィール目標表示 */
    .profile-target {
        background: #f0faf4;
        border-radius: 8px;
        padding: 0.8rem;
        margin: 0.5rem 0;
        font-size: 0.9rem;
    }
    .profile-target .pt-title {
        font-weight: 700;
        margin-bottom: 0.3rem;
    }
    .profile-target .pt-line {
        margin: 0.15rem 0;
        color: #2c3e50;
    }

    /* サイドバー 食事履歴 */
    .history-day {
        font-size: 0.85rem;
        color: #2c3e50;
        padding: 0.2rem 0;
    }

    /* Stage大会カード */
    .stage-card {
        background: linear-gradient(135deg, #f39c12 0%, #e67e22 100%);
        color: white;
        border-radius: 12px;
        padding: 1rem;
        margin: 0 0 1rem 0;
        text-align: center;
    }
    .stage-card h2 {
        margin: 0 0 0.3rem 0;
        font-size: 1.1rem;
        font-weight: 600;
    }
    .stage-card .stage-detail {
        font-size: 0.9rem;
        opacity: 0.9;
    }

    /* モバイル最適化 */
    @media (max-width: 640px) {
        .sage-header h1 { font-size: 1.8rem; }
        .total-card .cal-big { font-size: 2rem; }
        .remaining-card .remaining-cal { font-size: 1.7rem; }
        .pfc-item .value { font-size: 1.1rem; }
        .stage-card h2 { font-size: 1rem; }
        .stage-card .stage-detail { font-size: 0.85rem; }

        /* number_input のステッパーを大きく */
        .stNumberInput button {
            min-width: 36px;
            min-height: 36px;
        }
        /* expanderのタッチ領域を広げる */
        .streamlit-expanderHeader {
            padding: 0.8rem 1rem;
            min-height: 48px;
        }
        /* コピーテキスト周りの余白 */
        .stTextArea textarea {
            font-size: 0.85rem;
            padding: 12px;
        }
    }

    /* Streamlitデフォルト余白調整 */
    .block-container { padding-top: 1rem; }
</style>
""", unsafe_allow_html=True)


# ============================================================
# Gemini API設定
# ============================================================
def get_gemini_model():
    """Geminiモデルを初期化して返す"""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    genai.configure(api_key=api_key)
    return genai.GenerativeModel("gemini-2.5-flash")


def estimate_ingredient_pfc(model, name: str, gram: float) -> dict | None:
    """食材名とg数からPFCを推定"""
    prompt = f"""以下の食材のPFC・カロリーを推定してください。
食材: {name}
量: {gram}g

必ず以下のJSON形式のみで返してください:
{{"name": "{name}", "gram": {gram}, "calorie": 数値, "protein": 数値, "fat": 数値, "carb": 数値}}
"""
    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except Exception:
        return None


def analyze_meal_image(model, image: Image.Image) -> dict | None:
    """食事画像をGeminiに送信してPFC分析結果を取得"""
    prompt = """この食事の写真を分析してください。
含まれる料理名・食材を特定し、それぞれの推定グラム数、カロリー(kcal)、タンパク質(g)、脂質(g)、炭水化物(g)を推定してください。

必ず以下のJSON形式のみで返してください。説明文やマークダウンは不要です:
{
  "dishes": [
    {
      "name": "料理名",
      "ingredients": [
        {
          "name": "食材名",
          "gram": 推定グラム数,
          "calorie": カロリー数値,
          "protein": タンパク質グラム数値,
          "fat": 脂質グラム数値,
          "carb": 炭水化物グラム数値
        }
      ]
    }
  ]
}

注意:
- 数値は小数点第1位まで
- 写真に食事が写っていない場合は {"error": "食事が見つかりません"} を返してください
- 日本語で料理名・食材名を記載してください
"""
    try:
        response = model.generate_content([prompt, image])
        text = response.text.strip()
        # マークダウンのコードブロックを除去
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except json.JSONDecodeError:
        st.error("AIの応答を解析できませんでした。もう一度撮影してみてください。")
        return None
    except Exception as e:
        st.error(f"分析中にエラーが発生しました: {e}")
        return None


# ============================================================
# プロフィール・履歴のlocalStorage読み込み
# ============================================================
_all_ls = ls_get_all(["sage_profile", "sage_meals", "sage_training_logs", "sage_custom_exercises", "sage_cardio_logs", "sage_training_sessions", "sage_weight_log", "sage_auth_token"])
profile_data = _all_ls["sage_profile"]
meals_data = _all_ls["sage_meals"]
training_logs_data = _all_ls["sage_training_logs"]
custom_exercises_data = _all_ls["sage_custom_exercises"]
cardio_logs_data = _all_ls["sage_cardio_logs"]
training_sessions_data = _all_ls["sage_training_sessions"]
weight_log_data = _all_ls["sage_weight_log"]

# session_stateにキャッシュ（再レンダリング時のNone対策）
if profile_data is not None:
    st.session_state["_cached_profile"] = profile_data
elif "_cached_profile" not in st.session_state:
    st.session_state["_cached_profile"] = None

if meals_data is not None:
    st.session_state["_cached_meals"] = meals_data
elif "_cached_meals" not in st.session_state:
    st.session_state["_cached_meals"] = None
# 既にsession_stateにデータがあるのにlocalStorageがNoneの場合は上書きしない（JS読み込み遅延対策）

if training_logs_data is not None:
    st.session_state["_cached_training_logs"] = training_logs_data
elif "_cached_training_logs" not in st.session_state:
    st.session_state["_cached_training_logs"] = None

if custom_exercises_data is not None:
    st.session_state["_cached_custom_exercises"] = custom_exercises_data
elif "_cached_custom_exercises" not in st.session_state:
    st.session_state["_cached_custom_exercises"] = None

if cardio_logs_data is not None:
    st.session_state["_cached_cardio_logs"] = cardio_logs_data
elif "_cached_cardio_logs" not in st.session_state:
    st.session_state["_cached_cardio_logs"] = None

if training_sessions_data is not None:
    st.session_state["_cached_training_sessions"] = training_sessions_data
elif "_cached_training_sessions" not in st.session_state:
    st.session_state["_cached_training_sessions"] = None

if weight_log_data is not None:
    st.session_state["_cached_weight_log"] = weight_log_data
elif "_cached_weight_log" not in st.session_state:
    st.session_state["_cached_weight_log"] = None

cached_profile = st.session_state["_cached_profile"]
cached_meals = st.session_state["_cached_meals"] or {}
cached_training_logs = st.session_state["_cached_training_logs"] or []
cached_custom_exercises = st.session_state["_cached_custom_exercises"] or {
    "胸": [], "背中": [], "肩": [], "腕": [], "脚": [], "腹": []
}
cached_weight_log = st.session_state["_cached_weight_log"] or []

# ============================================================
# セッション自動復元（localStorageのトークンから）
# ============================================================
auth_token_data = _all_ls.get("sage_auth_token")
if auth_token_data and not st.session_state.get("supabase_session") and not st.session_state.get("_auth_restore_tried"):
    st.session_state["_auth_restore_tried"] = True
    try:
        _sb = get_supabase()
        if _sb and auth_token_data.get("access_token") and auth_token_data.get("refresh_token"):
            restored = _sb.auth.set_session(
                auth_token_data["access_token"],
                auth_token_data["refresh_token"],
            )
            if restored and restored.session:
                st.session_state["supabase_session"] = restored.session
                # トークンが更新された場合は保存
                new_token = {
                    "access_token": restored.session.access_token,
                    "refresh_token": restored.session.refresh_token,
                }
                if new_token != auth_token_data:
                    ls_set("sage_auth_token", new_token)
    except Exception:
        # トークンが無効（期限切れ等）→ クリア
        ls_set("sage_auth_token", None)


# ============================================================
# サイドバー
# ============================================================
with st.sidebar:
    # --- アカウント（Supabase Auth） ---
    sb_client = get_supabase()
    if sb_client:
        st.markdown("##### 👤 アカウント")

        if st.session_state.get("supabase_session"):
            # ログイン済み
            user_email = st.session_state["supabase_session"].user.email
            st.caption(f"ログイン中: {user_email}")
            if st.button("ログアウト", key="auth_logout"):
                try:
                    sb_client.auth.sign_out()
                except Exception:
                    pass
                st.session_state.pop("supabase_session", None)
                st.session_state.pop("_auth_restore_tried", None)
                ls_set("sage_auth_token", None)
                st.rerun()
        else:
            # 未ログイン
            auth_tab_login, auth_tab_signup = st.tabs(["ログイン", "新規登録"])
            with auth_tab_login:
                login_email = st.text_input("メールアドレス", key="login_email", placeholder="email@example.com")
                login_password = st.text_input("パスワード", type="password", key="login_password")
                if st.button("ログイン", key="auth_login_btn"):
                    if login_email and login_password:
                        try:
                            resp = sb_client.auth.sign_in_with_password({"email": login_email, "password": login_password})
                            st.session_state["supabase_session"] = resp.session
                            # セッショントークンをlocalStorageに保存（リロード後の自動復元用）
                            if resp.session:
                                ls_set("sage_auth_token", {
                                    "access_token": resp.session.access_token,
                                    "refresh_token": resp.session.refresh_token,
                                })
                            # Load data from Supabase and merge
                            if resp.session:
                                cloud_data = db_load(sb_client, resp.session.user.id)
                                if cloud_data:
                                    # Supabase wins for conflicts
                                    col_to_ls = {
                                        "profile": "sage_profile",
                                        "meals": "sage_meals",
                                        "training_logs": "sage_training_logs",
                                        "cardio_logs": "sage_cardio_logs",
                                        "training_sessions": "sage_training_sessions",
                                        "weight_log": "sage_weight_log",
                                        "custom_exercises": "sage_custom_exercises",
                                    }
                                    col_to_cache = {
                                        "profile": "_cached_profile",
                                        "meals": "_cached_meals",
                                        "training_logs": "_cached_training_logs",
                                        "cardio_logs": "_cached_cardio_logs",
                                        "training_sessions": "_cached_training_sessions",
                                        "weight_log": "_cached_weight_log",
                                        "custom_exercises": "_cached_custom_exercises",
                                    }
                                    for col, ls_key in col_to_ls.items():
                                        cloud_val = cloud_data.get(col)
                                        if cloud_val is not None:
                                            # Write to localStorage queue
                                            if "_ls_pending_writes" not in st.session_state:
                                                st.session_state["_ls_pending_writes"] = {}
                                            st.session_state["_ls_pending_writes"][ls_key] = cloud_val
                                            # Update session cache
                                            st.session_state[col_to_cache[col]] = cloud_val
                            st.rerun()
                        except Exception as e:
                            err_msg = str(e)
                            st.error(f"ログイン失敗: {err_msg}")
                    else:
                        st.warning("メールアドレスとパスワードを入力してください")
            with auth_tab_signup:
                signup_email = st.text_input("メールアドレス", key="signup_email", placeholder="email@example.com")
                signup_password = st.text_input("パスワード", type="password", key="signup_password")
                if st.button("新規登録", key="auth_signup_btn"):
                    if signup_email and signup_password:
                        try:
                            resp = sb_client.auth.sign_up({"email": signup_email, "password": signup_password})
                            if resp.session:
                                st.session_state["supabase_session"] = resp.session
                                ls_set("sage_auth_token", {
                                    "access_token": resp.session.access_token,
                                    "refresh_token": resp.session.refresh_token,
                                })
                                st.success("登録完了！")
                                st.rerun()
                            else:
                                st.info("確認メールを送信しました。メール内のリンクをクリックしてください。")
                        except Exception as e:
                            err_msg = str(e)
                            st.error(f"登録失敗: {err_msg}")
                    else:
                        st.warning("メールアドレスとパスワードを入力してください")
            with st.expander("パスワードを忘れた場合"):
                reset_email = st.text_input("メールアドレス", key="reset_email", placeholder="登録メールアドレス")
                if st.button("リセットメール送信", key="auth_reset_btn"):
                    if reset_email:
                        try:
                            sb_client.auth.reset_password_for_email(reset_email, {"redirect_to": "https://genkairyoshi-sage.hf.space"})
                            st.success("パスワードリセットメールを送信しました。")
                        except Exception as e:
                            st.error(f"送信失敗: {e}")
                    else:
                        st.warning("メールアドレスを入力してください")
            st.caption("アカウントなしでも利用可（データはブラウザのみに保存）")

        st.divider()

    # --- アクセスコード ---
    st.markdown("### 🔑 プラン")
    access_code = st.text_input(
        "アクセスコード",
        type="default",
        key="access_code",
        placeholder="コードを入力",
        label_visibility="collapsed",
    )
    # コード認証: Stage > Trainee > Standard
    is_stage = access_code == STAGE_CODE
    is_trainee = access_code == TRAINEE_CODE or is_stage
    is_standard = access_code == STANDARD_CODE or is_trainee
    if access_code and not is_standard:
        st.error("コードが正しくありません")
    elif is_stage:
        st.success("✅ Pro プラン有効")
    elif is_trainee:
        st.success("✅ Gym プラン有効")
    elif is_standard:
        st.success("✅ Basic プラン有効")

    st.divider()

    # --- モード切替 ---
    app_mode = st.radio(
        "モード",
        options=["🍽️ 食事", "🏋️ トレーニング", "📊 レポート", "⚙️ 設定"],
        key="app_mode",
        horizontal=True,
        label_visibility="collapsed",
    )
    st.divider()

    # localStorage接続状態 + クラウド同期ステータス
    if st.session_state.get("_ls_load_success", False):
        if st.session_state.get("supabase_session"):
            st.caption("🟢 データ接続OK / ☁️ クラウド同期ON")
        else:
            st.caption("🟢 データ接続OK / ☁️ 未ログイン（ローカルのみ）")
    else:
        st.warning("🔴 データ未接続 — リロードしてください")
    # 直近の書き込みステータス
    ws = st.session_state.get("_ls_write_status")
    if ws:
        st.caption(f"書込: {ws}")

    profile_saved = is_standard and st.session_state["_cached_profile"] is not None

# ============================================================
# メインUI
# ============================================================
st.markdown(
    '<div class="sage-header">'
    '<h1>🌿 SAGE</h1>'
    '<p>食事の写真を撮るだけ。AIが賢く分析。</p>'
    '</div>',
    unsafe_allow_html=True,
)
st.caption("※ ブラウザアプリは最新版にアップデートしてご利用ください")
# リロードボタン（親DOMに直接注入）
import streamlit.components.v1 as components
components.html(
    """
    <script>
        (function() {
            if (window.parent.document.getElementById('sage-reload-btn')) return;
            var btn = window.parent.document.createElement('button');
            btn.id = 'sage-reload-btn';
            btn.textContent = '🔄';
            btn.style.cssText = 'position:fixed;top:12px;right:16px;z-index:9999;background:white;border:1px solid #ddd;border-radius:8px;padding:6px 12px;font-size:1.2rem;cursor:pointer;color:#7f8c8d;box-shadow:0 1px 3px rgba(0,0,0,0.1);';
            btn.addEventListener('click', function() { window.parent.location.reload(); });
            window.parent.document.body.appendChild(btn);
        })();
    </script>
    """,
    height=0,
)

# APIキーチェック
model = get_gemini_model()
if model is None:
    st.warning(
        "⚠️ GEMINI_API_KEY が設定されていません。\n\n"
        "環境変数 `GEMINI_API_KEY` にGoogle AI StudioのAPIキーを設定してください。\n\n"
        "ローカル環境では `.env` ファイルに以下を記載:\n"
        "```\nGEMINI_API_KEY=your_api_key_here\n```"
    )
    st.stop()

# ============================================================
# 有酸素種目METデータベース
# ============================================================
CARDIO_DB = {
    "エアロバイク（軽め）": 4.0,
    "エアロバイク（中強度）": 6.8,
    "エアロバイク（高強度）": 10.0,
    "ランニング（8km/h）": 8.3,
    "ランニング（10km/h）": 10.0,
    "ランニング（12km/h）": 11.8,
    "トレッドミル（軽め）": 4.0,
    "トレッドミル（中強度）": 8.3,
    "トレッドミル（高強度）": 10.0,
    "ウォーキング（普通）": 3.5,
    "ウォーキング（速歩）": 5.0,
    "水泳（クロール・ゆっくり）": 5.8,
    "水泳（クロール・速め）": 9.8,
    "縄跳び": 11.0,
    "階段昇降": 6.0,
    "ステッパー": 6.0,
}


def render_cardio_log():
    """有酸素ログのUI"""
    cur_cardio_logs = st.session_state.get("_cached_cardio_logs") or []

    # --- 新規記録 ---
    cardio_date_str = st.text_input(
        "記録日", key="cardio_date",
        value=now_jst().strftime("%Y-%m-%d"),
        placeholder="YYYY-MM-DD",
    )

    cardio_types = list(CARDIO_DB.keys()) + ["＋ カスタム"]
    selected_cardio = st.selectbox("種目を選択", cardio_types, key="cardio_type")

    actual_cardio = selected_cardio
    custom_met = None
    if selected_cardio == "＋ カスタム":
        actual_cardio = st.text_input("種目名", key="cardio_custom_name", placeholder="例: ボクシング")
        custom_met = st.number_input("MET値", min_value=1.0, value=5.0, step=0.5, key="cardio_custom_met")

    duration = st.number_input("時間（分）", min_value=1, value=None, step=1, key="cardio_duration", placeholder="30")
    memo = st.text_input("メモ", key="cardio_memo", placeholder="例: 心拍130前後、負荷8")

    # カロリー計算プレビュー
    met = custom_met if selected_cardio == "＋ カスタム" and custom_met else CARDIO_DB.get(selected_cardio, 5.0)
    profile = st.session_state.get("_cached_profile")
    body_weight = profile.get("weight", 70.0) if profile else 70.0
    if duration:
        cal_burn = met * body_weight * (duration / 60)
        st.markdown(f"**推定消費: {cal_burn:.0f}kcal**（MET {met} × {body_weight}kg × {duration}分）")

    # 記録する・クリアボタン
    cardio_btn_col1, cardio_btn_col2 = st.columns(2)
    with cardio_btn_col2:
        if st.button("🗑 クリア", key="cardio_clear_btn", use_container_width=True):
            st.rerun()
    with cardio_btn_col1:
        cardio_save_pressed = st.button("💾 記録する", key="cardio_save", use_container_width=True)
    if cardio_save_pressed:
        if not actual_cardio or not duration:
            st.error("種目と時間を入力してください")
        else:
            cal_burn = met * body_weight * (duration / 60)
            new_log = {
                "date": cardio_date_str,
                "type": actual_cardio,
                "duration": duration,
                "met": met,
                "calories": round(cal_burn),
                "memo": memo or "",
            }
            all_cardio = list(cur_cardio_logs)
            all_cardio.append(new_log)
            ls_set("sage_cardio_logs", all_cardio)
            st.session_state["_cached_cardio_logs"] = all_cardio
            st.success(f"✅ {actual_cardio} {duration}分 / {cal_burn:.0f}kcal を記録しました")
            st.rerun()

    # --- 過去の記録 ---
    st.markdown("---")
    st.markdown("**📅 有酸素履歴（直近14日）**")

    if cur_cardio_logs:
        logs_by_date = {}
        for log in cur_cardio_logs:
            d = log.get("date", "不明")
            if d not in logs_by_date:
                logs_by_date[d] = []
            logs_by_date[d].append(log)

        sorted_dates = sorted(logs_by_date.keys(), reverse=True)[:14]
        today_str = now_jst().strftime("%Y-%m-%d")

        for date_str in sorted_dates:
            day_logs = logs_by_date[date_str]
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                month_day = f"{dt.month}/{dt.day}"
            except ValueError:
                month_day = date_str

            is_today = date_str == today_str
            total_cal = sum(l.get("calories", 0) for l in day_logs)
            total_min = sum(l.get("duration", 0) for l in day_logs)

            title = f"{month_day}{'（今日）' if is_today else ''} {len(day_logs)}種目 / {total_min}分 / {total_cal}kcal"

            with st.expander(title, expanded=is_today):
                for li, log in enumerate(day_logs):
                    ctype = log.get("type", "")
                    dur = log.get("duration", 0)
                    cal = log.get("calories", 0)
                    memo_text = log.get("memo", "")
                    memo_disp = f"  {memo_text}" if memo_text else ""
                    st.caption(f"　{ctype} — {dur}分 / {cal}kcal{memo_disp}")

                    if st.button(f"🗑 削除", key=f"del_cardio_{date_str}_{li}"):
                        all_cardio = list(cur_cardio_logs)
                        count = 0
                        for idx, orig in enumerate(all_cardio):
                            if orig.get("date") == date_str:
                                if count == li:
                                    all_cardio.pop(idx)
                                    break
                                count += 1
                        ls_set("sage_cardio_logs", all_cardio)
                        st.session_state["_cached_cardio_logs"] = all_cardio
                        st.rerun()
    else:
        st.caption("まだ記録がありません")


# ============================================================
# トレーニングログ描画関数（複数箇所から呼ぶため関数化）
# ============================================================
def render_training_log():
    """トレーニングログのUI全体を描画する"""

    # 最新のキャッシュを取得
    cur_training_logs = st.session_state.get("_cached_training_logs") or []
    cur_custom_exercises = st.session_state.get("_cached_custom_exercises") or {
        "胸": [], "背中": [], "肩": [], "腕": [], "脚": [], "腹": []
    }

    # --- 新規記録 ---
    train_date_str = st.text_input(
        "記録日", key="train_date",
        value=now_jst().strftime("%Y-%m-%d"),
        placeholder="YYYY-MM-DD",
    )
    body_parts = list(EXERCISE_DB.keys())
    selected_part = st.selectbox("部位を選択", body_parts, key="train_body_part")

    # 種目リスト = DB + カスタム
    base_exercises = EXERCISE_DB.get(selected_part, [])
    custom_for_part = cur_custom_exercises.get(selected_part, [])
    all_exercises = base_exercises + [e for e in custom_for_part if e not in base_exercises]

    selected_exercise = st.selectbox("種目を選択", all_exercises, key="train_exercise")
    actual_exercise = selected_exercise

    # 種目登録フォーム
    if st.button("＋ 種目登録", key="train_add_custom_btn"):
        st.session_state["_show_custom_exercise_form"] = True
    if st.session_state.get("_show_custom_exercise_form"):
        custom_name = st.text_input("種目名を入力", key="train_custom_name", placeholder="例: スミスマシンベンチ")
        reg_col1, reg_col2 = st.columns(2)
        with reg_col1:
            if st.button("登録", key="train_register_custom", use_container_width=True):
                if custom_name:
                    updated_custom = dict(cur_custom_exercises)
                    if selected_part not in updated_custom:
                        updated_custom[selected_part] = []
                    if custom_name not in updated_custom[selected_part]:
                        updated_custom[selected_part].append(custom_name)
                        ls_set("sage_custom_exercises", updated_custom)
                        st.session_state["_cached_custom_exercises"] = updated_custom
                    st.session_state["_show_custom_exercise_form"] = False
                    st.success(f"✅ {custom_name} を登録しました")
                    st.rerun()
                else:
                    st.warning("種目名を入力してください")
        with reg_col2:
            if st.button("キャンセル", key="train_cancel_custom", use_container_width=True):
                st.session_state["_show_custom_exercise_form"] = False
                st.rerun()

    # 前回の記録を表示
    if actual_exercise:
        prev = [log for log in reversed(cur_training_logs)
                if log.get("exercise") == actual_exercise]
        if prev:
            last = prev[0]
            sets_text = " / ".join(f"{s.get('weight', 0):g}kg×{s.get('reps', 0)}" for s in last.get("sets", []))
            st.caption(f"📋 前回（{last.get('date', '')}）: {sets_text}")

    # セット入力
    if "train_sets" not in st.session_state:
        st.session_state.train_sets = [{"weight": 0.0, "reps": 0, "note": ""}]

    st.markdown("**セット入力**")

    sets_to_display = st.session_state.train_sets
    for si, s in enumerate(sets_to_display):
        st.caption(f"セット {si + 1}")
        col_w, col_r = st.columns(2)
        with col_w:
            w_val = s.get("weight", 0.0)
            w = st.number_input(
                f"重量 kg（セット{si+1}）", min_value=0.0, value=None if w_val == 0.0 else float(w_val),
                step=0.25, format="%.2f", key=f"train_w_{si}",
                label_visibility="collapsed", placeholder="重量 (kg)",
            )
            sets_to_display[si]["weight"] = w or 0.0
        with col_r:
            r_val = s.get("reps", 0)
            r = st.number_input(
                f"回数(Rep)（セット{si+1}）", min_value=0, value=None if r_val == 0 else int(r_val),
                step=1, key=f"train_r_{si}",
                label_visibility="collapsed", placeholder="回数(Rep)",
            )
            sets_to_display[si]["reps"] = r or 0
        n = st.text_input(
            f"メモ（セット{si+1}）", value=s.get("note", ""),
            key=f"train_n_{si}",
            label_visibility="collapsed", placeholder="メモ（任意）",
        )
        sets_to_display[si]["note"] = n

    # セット追加・削除ボタン
    col_add_set, col_remove_set = st.columns(2)
    with col_add_set:
        if st.button("➕ セット追加", key="train_add_set"):
            st.session_state.train_sets.append({"weight": 0.0, "reps": 0, "note": ""})
            st.rerun()
    with col_remove_set:
        if len(st.session_state.train_sets) > 1:
            if st.button("➖ 最後のセットを削除", key="train_remove_set"):
                st.session_state.train_sets.pop()
                st.rerun()

    # 記録する・クリアボタン
    train_btn_col1, train_btn_col2 = st.columns(2)
    with train_btn_col2:
        if st.button("🗑 クリア", key="train_clear_btn", use_container_width=True):
            st.session_state.train_sets = [{"weight": 0.0, "reps": 0, "note": ""}]
            st.rerun()
    with train_btn_col1:
        train_save_pressed = st.button("💾 記録する", key="train_save", use_container_width=True)
    if train_save_pressed:
        if not actual_exercise:
            st.error("種目を選択または入力してください")
        else:
            # セットデータを構築（重量>0 or 回数(Rep)>0 のセットのみ保存）
            valid_sets = []
            for s in st.session_state.train_sets:
                if s["weight"] > 0 or s["reps"] > 0:
                    valid_sets.append({
                        "weight": s["weight"],
                        "reps": s["reps"],
                        "note": s["note"],
                    })

            if not valid_sets:
                st.error("少なくとも1セットのデータを入力してください")
            else:
                new_log = {
                    "date": train_date_str,
                    "body_part": selected_part,
                    "exercise": actual_exercise,
                    "sets": valid_sets,
                }

                all_logs = list(cur_training_logs)
                all_logs.append(new_log)

                ls_set("sage_training_logs", all_logs)
                st.session_state["_cached_training_logs"] = all_logs
                # セットをリセット
                st.session_state.train_sets = [{"weight": 0.0, "reps": 0, "note": ""}]
                st.success(f"✅ {selected_part} / {actual_exercise} を記録しました")
                st.rerun()

    # --- 一括インポート ---
    st.markdown("---")
    with st.expander("📋 一括インポート", expanded=False):
        st.markdown("1行ずつ `種目名 重量×回数(Rep)` の形式で入力。部位は上の選択を使用。")
        st.caption("例:\nベンチプレス 90×6 90×5 80×10\nディップス 32.5×12 33.75×8")
        bulk_train_text = st.text_area("一括入力", key="bulk_train_text", height=150,
            placeholder="ベンチプレス 90×6 90×5 80×10\nディップス 32.5×12 33.75×8")
        if st.button("📋 一括登録", key="bulk_train_btn", use_container_width=True):
            if not bulk_train_text:
                st.warning("テキストを入力してください")
            else:
                lines = [l.strip() for l in bulk_train_text.strip().split("\n") if l.strip()]
                imported = 0
                errors = []
                all_logs = list(cur_training_logs)

                for li, line in enumerate(lines):
                    try:
                        parts = line.split()
                        if len(parts) < 2:
                            errors.append(f"行{li+1}: 項目不足 → {line}")
                            continue
                        exercise_name = parts[0]
                        sets = []
                        for sp in parts[1:]:
                            # 90×6 or 90x6 形式
                            sp = sp.replace("x", "×").replace("X", "×")
                            if "×" in sp:
                                w_str, r_str = sp.split("×", 1)
                                sets.append({"weight": float(w_str), "reps": int(r_str), "note": ""})
                            else:
                                errors.append(f"行{li+1}: セット形式エラー → {sp}")
                        if sets:
                            all_logs.append({
                                "date": train_date_str,
                                "body_part": selected_part,
                                "exercise": exercise_name,
                                "sets": sets,
                            })
                            imported += 1
                    except (ValueError, IndexError):
                        errors.append(f"行{li+1}: パース失敗 → {line}")

                if imported > 0:
                    ls_set("sage_training_logs", all_logs)
                    st.session_state["_cached_training_logs"] = all_logs
                    st.success(f"✅ {imported}種目を登録しました")
                    st.rerun()
                if errors:
                    for e in errors:
                        st.warning(e)

    # --- セッション時間（消費カロリー概算）---
    st.markdown("---")
    st.markdown("**⏱ セッション時間**")
    cur_sessions = st.session_state.get("_cached_training_sessions") or {}
    existing_session = cur_sessions.get(train_date_str, {})

    session_duration = st.number_input(
        "トレーニング時間（分）", min_value=1,
        value=existing_session.get("duration") if existing_session.get("duration") else None,
        step=1, key="session_duration", placeholder="例: 90"
    )

    MET_OPTIONS = {
        "ゆるめ（マシン中心・休憩多め）": 3.5,
        "ふつう": 5.0,
        "きつめ（フリーウエイト・短休憩）": 6.0,
        "追い込み（サーキット・休憩なし）": 8.0,
    }
    existing_met = existing_session.get("met", 5.0)
    default_idx = list(MET_OPTIONS.values()).index(existing_met) if existing_met in MET_OPTIONS.values() else 1
    selected_intensity = st.selectbox(
        "強度", list(MET_OPTIONS.keys()), index=default_idx, key="session_intensity"
    )
    session_met = MET_OPTIONS[selected_intensity]

    if session_duration:
        profile = st.session_state.get("_cached_profile")
        body_weight = profile.get("weight", 70.0) if profile else 70.0
        session_cal = session_met * body_weight * (session_duration / 60)
        st.markdown(f"**推定消費: {session_cal:.0f}kcal**")

        if st.button("💾 セッション時間を保存", key="save_session_duration", use_container_width=True):
            cur_sessions[train_date_str] = {
                "duration": session_duration,
                "calories": round(session_cal),
                "met": session_met,
            }
            ls_set("sage_training_sessions", cur_sessions)
            st.session_state["_cached_training_sessions"] = cur_sessions
            st.success(f"✅ {train_date_str}のセッション時間を保存しました")
            st.rerun()

    # --- 過去の記録閲覧 ---
    st.markdown("---")
    st.markdown("**📅 トレーニング履歴（直近14日）**")

    if cur_training_logs:
        # 日付別にグループ化
        logs_by_date = {}
        for log in cur_training_logs:
            d = log.get("date", "不明")
            if d not in logs_by_date:
                logs_by_date[d] = []
            logs_by_date[d].append(log)

        sorted_dates = sorted(logs_by_date.keys(), reverse=True)[:14]
        today_str = now_jst().strftime("%Y-%m-%d")

        for date_str in sorted_dates:
            day_logs = logs_by_date[date_str]
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                month_day = f"{dt.month}/{dt.day}"
            except ValueError:
                month_day = date_str

            is_today = date_str == today_str

            # 部位一覧（重複排除・順序保持）
            seen_parts = []
            for log in day_logs:
                p = log.get("body_part", "")
                if p and p not in seen_parts:
                    seen_parts.append(p)
            parts_label = "・".join(seen_parts) if seen_parts else ""

            exercise_count = len(day_logs)
            total_sets = sum(len(log.get("sets", [])) for log in day_logs)
            total_volume = sum(
                s.get("weight", 0) * s.get("reps", 0)
                for log in day_logs
                for s in log.get("sets", [])
            )
            cur_sessions_title = st.session_state.get("_cached_training_sessions") or {}
            session_info_title = cur_sessions_title.get(date_str, {})
            session_cal_title = session_info_title.get("calories", 0)
            cal_suffix = f" / {session_cal_title}kcal" if session_cal_title else ""
            expander_title = f"{month_day}（{parts_label}）{exercise_count}種目 / {total_sets}set / {total_volume:,}kg{cal_suffix}" if parts_label else f"{month_day}　{exercise_count}種目 / {total_sets}set / {total_volume:,}kg{cal_suffix}"
            if is_today:
                expander_title = f"{month_day}（今日 / {parts_label}）{exercise_count}種目 / {total_sets}set / {total_volume:,}kg{cal_suffix}" if parts_label else f"{month_day}（今日）{exercise_count}種目 / {total_sets}set / {total_volume:,}kg{cal_suffix}"

            with st.expander(expander_title, expanded=is_today):
                cur_sessions_hist = st.session_state.get("_cached_training_sessions") or {}
                session_info = cur_sessions_hist.get(date_str, {})
                session_cal = session_info.get("calories", 0)
                session_dur = session_info.get("duration", 0)
                cal_text = f" / {session_dur}分 / 約{session_cal}kcal消費" if session_cal else ""
                st.caption(f"📊 {exercise_count}種目 / {total_sets}set / 総挙上重量 {total_volume:,}kg{cal_text}")
                # 部位ごとにグループ化して表示
                current_part = None
                for li, log in enumerate(day_logs):
                    part = log.get("body_part", "")
                    exercise = log.get("exercise", "")
                    sets = log.get("sets", [])

                    # 部位が変わったら見出し表示
                    if part != current_part:
                        if current_part is not None:
                            st.markdown("")  # 部位間にスペース
                        st.markdown(f"**【{part}】**")
                        current_part = part

                    # 種目名
                    st.markdown(f"　{exercise}")

                    # セット一覧
                    for s in sets:
                        w = s.get("weight", 0)
                        r = s.get("reps", 0)
                        note = s.get("note", "")
                        note_text = f"  {note}" if note else ""
                        # 重量の表示: 整数ならint表示、小数点以下あればそのまま
                        w_str = f"{w:g}kg" if w > 0 else "自重"
                        st.caption(f"　　{w_str} × {r}{note_text}")

                    edit_key = f"edit_train_{date_str}_{li}"
                    add_set_key = f"hist_add_{date_str}_{li}"
                    col_edit, col_add, col_del = st.columns([1, 1, 1])
                    with col_edit:
                        if st.button("✏️", key=f"btn_edit_{date_str}_{li}"):
                            st.session_state[edit_key] = True
                    with col_add:
                        if st.button("➕", key=f"btn_add_{date_str}_{li}"):
                            st.session_state[add_set_key] = True
                            st.rerun()
                    with col_del:
                        if st.button("🗑", key=f"del_train_{date_str}_{li}"):
                            all_logs = list(cur_training_logs)
                            count = 0
                            for idx, orig_log in enumerate(all_logs):
                                if orig_log.get("date") == date_str:
                                    if count == li:
                                        all_logs.pop(idx)
                                        break
                                    count += 1
                            ls_set("sage_training_logs", all_logs)
                            st.session_state["_cached_training_logs"] = all_logs
                            st.rerun()

                    if st.session_state.get(edit_key):
                        st.markdown("---")
                        edited_sets = []
                        for si, s in enumerate(sets):
                            c1, c2, c3 = st.columns([2, 2, 3])
                            with c1:
                                new_w = st.number_input("重量(kg)", value=float(s.get("weight", 0)), min_value=0.0, step=2.5, key=f"ew_{date_str}_{li}_{si}")
                            with c2:
                                new_r = st.number_input("回数(Rep)", value=int(s.get("reps", 0)), min_value=0, step=1, key=f"er_{date_str}_{li}_{si}")
                            with c3:
                                new_n = st.text_input("メモ", value=s.get("note", ""), key=f"en_{date_str}_{li}_{si}")
                            edited_sets.append({"weight": new_w, "reps": new_r, "note": new_n})
                        col_save, col_cancel = st.columns([1, 1])
                        with col_save:
                            if st.button("💾 保存", key=f"save_edit_{date_str}_{li}"):
                                all_logs = list(cur_training_logs)
                                count = 0
                                for idx, orig_log in enumerate(all_logs):
                                    if orig_log.get("date") == date_str:
                                        if count == li:
                                            all_logs[idx]["sets"] = edited_sets
                                            break
                                        count += 1
                                ls_set("sage_training_logs", all_logs)
                                st.session_state["_cached_training_logs"] = all_logs
                                del st.session_state[edit_key]
                                st.rerun()
                        with col_cancel:
                            if st.button("❌ キャンセル", key=f"cancel_edit_{date_str}_{li}"):
                                del st.session_state[edit_key]
                                st.rerun()

                    if st.session_state.get(add_set_key):
                        col_hw, col_hr = st.columns(2)
                        with col_hw:
                            h_w = st.number_input("重量", min_value=0.0, value=None, step=0.25, format="%.2f",
                                                  key=f"haw_{date_str}_{li}", placeholder="kg", label_visibility="collapsed")
                        with col_hr:
                            h_r = st.number_input("回数(Rep)", min_value=0, value=None, step=1,
                                                  key=f"har_{date_str}_{li}", placeholder="回数(Rep)", label_visibility="collapsed")
                        h_n = st.text_input("メモ", key=f"han_{date_str}_{li}", placeholder="メモ（任意）", label_visibility="collapsed")
                        col_hs, col_hc = st.columns(2)
                        with col_hs:
                            if st.button("💾 追加", key=f"has_{date_str}_{li}", use_container_width=True):
                                if (h_w and h_w > 0) or (h_r and h_r > 0):
                                    all_logs = list(cur_training_logs)
                                    count = 0
                                    for idx, orig_log in enumerate(all_logs):
                                        if orig_log.get("date") == date_str:
                                            if count == li:
                                                all_logs[idx]["sets"].append({
                                                    "weight": h_w or 0.0, "reps": h_r or 0, "note": h_n or ""
                                                })
                                                break
                                            count += 1
                                    ls_set("sage_training_logs", all_logs)
                                    st.session_state["_cached_training_logs"] = all_logs
                                    st.session_state.pop(add_set_key, None)
                                    st.rerun()
                        with col_hc:
                            if st.button("✕", key=f"hac_{date_str}_{li}", use_container_width=True):
                                st.session_state.pop(add_set_key, None)
                                st.rerun()
    else:
        st.caption("まだ記録がありません")


# ============================================================
# モード分岐
# ============================================================
if app_mode == "⚙️ 設定":
    st.markdown("#### ⚙️ 設定")

    if is_standard:
        st.markdown("##### 📊 プロフィール設定")

        # デフォルト値（localStorage or 初期値）
        default_weight = 70.0
        default_goal = 65.0
        default_activity_idx = 0
        if cached_profile:
            default_weight = cached_profile.get("weight", 70.0)
            default_goal = cached_profile.get("goal_weight", 65.0)
            saved_activity = cached_profile.get("activity_label", "")
            activity_keys = list(ACTIVITY_LEVELS.keys())
            if saved_activity in activity_keys:
                default_activity_idx = activity_keys.index(saved_activity)

        prof_weight = st.number_input(
            "体重 (kg)",
            min_value=30.0,
            max_value=200.0,
            value=default_weight,
            step=0.1,
            format="%.1f",
            key="prof_weight",
        )
        prof_goal = st.number_input(
            "目標体重 (kg)",
            min_value=30.0,
            max_value=200.0,
            value=default_goal,
            step=0.1,
            format="%.1f",
            key="prof_goal",
        )
        prof_activity = st.selectbox(
            "活動レベル",
            options=list(ACTIVITY_LEVELS.keys()),
            index=default_activity_idx,
            key="prof_activity",
        )

        # 自動計算
        activity_factor = ACTIVITY_LEVELS[prof_activity]
        bmr = prof_weight * 23
        tdee = bmr * activity_factor

        # Stage: 目標タイプによるカロリー計算
        goal_type = "減量"  # デフォルト
        competition_date = None
        if is_stage:
            # デフォルト値（localStorageから復元）
            default_goal_type_idx = 0
            default_comp_date = None
            if cached_profile:
                saved_goal_type = cached_profile.get("goal_type", "reduce")
                if saved_goal_type == "bulk":
                    default_goal_type_idx = 1
                saved_comp_date = cached_profile.get("competition_date")
                if saved_comp_date:
                    try:
                        default_comp_date = datetime.strptime(saved_comp_date, "%Y-%m-%d").date()
                    except (ValueError, TypeError):
                        default_comp_date = None

            goal_type = st.selectbox(
                "目標タイプ",
                options=["減量", "増量"],
                index=default_goal_type_idx,
                key="goal_type",
            )
            # 大会日をテキスト入力（YYYY-MM-DD）
            default_comp_str = default_comp_date.strftime("%Y-%m-%d") if default_comp_date else ""
            competition_date_str = st.text_input(
                "大会日（YYYY-MM-DD）",
                value=default_comp_str,
                key="competition_date",
                placeholder="例: 2026-07-20",
            )
            competition_date = None
            if competition_date_str:
                try:
                    competition_date = datetime.strptime(competition_date_str.strip(), "%Y-%m-%d").date()
                except ValueError:
                    st.warning("日付はYYYY-MM-DD形式で入力してください")

        if is_stage and goal_type == "増量":
            auto_cal = tdee + 300
        else:
            auto_cal = tdee - 400

        auto_p = prof_weight * 2.0
        auto_f = auto_cal * 0.25 / 9
        auto_c = (auto_cal - auto_p * 4 - auto_f * 9) / 4

        # 保存済みの値があればそちらをデフォルトにする
        saved_cal = cached_profile.get("target_cal", round(auto_cal)) if cached_profile else round(auto_cal)
        saved_p = cached_profile.get("target_p", round(auto_p)) if cached_profile else round(auto_p)
        saved_f = cached_profile.get("target_f", round(auto_f)) if cached_profile else round(auto_f)
        saved_c = cached_profile.get("target_c", round(auto_c)) if cached_profile else round(auto_c)

        # 目標（編集可能）
        st.markdown("##### 📊 目標（編集可能）")
        st.caption(f"自動計算値: {auto_cal:.0f}kcal / P:{auto_p:.0f}g F:{auto_f:.0f}g C:{auto_c:.0f}g")
        target_cal = st.number_input("目標カロリー (kcal)", min_value=500, value=int(saved_cal), step=50, key="target_cal_input")
        tc1, tc2, tc3 = st.columns(3)
        with tc1:
            target_p = st.number_input("P (g)", min_value=0, value=int(saved_p), step=5, key="target_p_input")
        with tc2:
            target_f = st.number_input("F (g)", min_value=0, value=int(saved_f), step=5, key="target_f_input")
        with tc3:
            target_c = st.number_input("C (g)", min_value=0, value=int(saved_c), step=5, key="target_c_input")

        # --- トレ日/オフ日カロリー自動切替（Gym以上） ---
        if is_trainee:
            st.markdown("---")
            st.markdown("##### 🔄 トレ日/オフ日カロリー自動切替")
            default_auto_switch = cached_profile.get("auto_switch", False) if cached_profile else False
            auto_switch = st.checkbox("トレ日/オフ日で目標を自動切替", value=default_auto_switch, key="auto_switch_toggle")

            if auto_switch:
                st.caption("トレ日: 基準+200kcal（炭水化物増）/ オフ日: 基準-100kcal")
                # デフォルト値
                default_train_target = cached_profile.get("target_train", {}) if cached_profile else {}
                default_rest_target = cached_profile.get("target_rest", {}) if cached_profile else {}

                train_cal_default = default_train_target.get("cal", target_cal + 200)
                train_p_default = default_train_target.get("p", target_p)
                train_f_default = default_train_target.get("f", target_f)
                train_c_default = default_train_target.get("c", target_c + 50)

                rest_cal_default = default_rest_target.get("cal", target_cal - 100)
                rest_p_default = default_rest_target.get("p", target_p)
                rest_f_default = default_rest_target.get("f", target_f)
                rest_c_default = default_rest_target.get("c", target_c - 25)

                st.markdown("**トレ日**")
                train_cal = st.number_input("カロリー (kcal)", min_value=500, value=int(train_cal_default), step=50, key="train_cal_input")
                tr1, tr2, tr3 = st.columns(3)
                with tr1:
                    train_p = st.number_input("P (g)", min_value=0, value=int(train_p_default), step=5, key="train_p_input")
                with tr2:
                    train_f = st.number_input("F (g)", min_value=0, value=int(train_f_default), step=5, key="train_f_input")
                with tr3:
                    train_c = st.number_input("C (g)", min_value=0, value=int(train_c_default), step=5, key="train_c_input")

                st.markdown("**オフ日**")
                rest_cal = st.number_input("カロリー (kcal)", min_value=500, value=int(rest_cal_default), step=50, key="rest_cal_input")
                re1, re2, re3 = st.columns(3)
                with re1:
                    rest_p = st.number_input("P (g)", min_value=0, value=int(rest_p_default), step=5, key="rest_p_input")
                with re2:
                    rest_f = st.number_input("F (g)", min_value=0, value=int(rest_f_default), step=5, key="rest_f_input")
                with re3:
                    rest_c = st.number_input("C (g)", min_value=0, value=int(rest_c_default), step=5, key="rest_c_input")

        # プロフィール保存ボタン
        if st.button("💾 プロフィールを保存", key="save_profile"):
            new_profile = {
                "weight": prof_weight,
                "goal_weight": prof_goal,
                "activity_label": prof_activity,
                "activity_factor": activity_factor,
                "target_cal": target_cal,
                "target_p": target_p,
                "target_f": target_f,
                "target_c": target_c,
            }
            # Stage: 大会情報も保存
            if is_stage:
                new_profile["goal_type"] = "bulk" if goal_type == "増量" else "reduce"
                new_profile["competition_date"] = competition_date.strftime("%Y-%m-%d") if competition_date else None
            # トレ日/オフ日自動切替
            if is_trainee:
                new_profile["auto_switch"] = auto_switch
                if auto_switch:
                    new_profile["target_train"] = {"cal": train_cal, "p": train_p, "f": train_f, "c": train_c}
                    new_profile["target_rest"] = {"cal": rest_cal, "p": rest_p, "f": rest_f, "c": rest_c}
            ls_set("sage_profile", new_profile)
            st.session_state["_cached_profile"] = new_profile

            # 体重ログに今日の体重を追記/更新
            today_str_wl = now_jst().strftime("%Y-%m-%d")
            cur_weight_log = list(st.session_state.get("_cached_weight_log") or [])
            # 今日のエントリがあれば更新、なければ追加
            found_today = False
            for wl_entry in cur_weight_log:
                if wl_entry.get("date") == today_str_wl:
                    wl_entry["weight"] = prof_weight
                    found_today = True
                    break
            if not found_today:
                cur_weight_log.append({"date": today_str_wl, "weight": prof_weight})
            ls_set("sage_weight_log", cur_weight_log)
            st.session_state["_cached_weight_log"] = cur_weight_log

            st.success("保存しました")

        # --- Stage: 大会・目標管理表示 ---
        if is_stage:
            st.markdown("---")
            st.markdown("##### 🎯 大会・目標管理")

            if competition_date:
                today = now_jst().date()
                days_left = (competition_date - today).days
                weight_diff = prof_weight - prof_goal if goal_type == "減量" else prof_goal - prof_weight
                weeks_left = max(days_left / 7, 0.1)  # ゼロ割り防止

                st.markdown(f"📅 大会まで: **{days_left}日**")
                if goal_type == "減量":
                    st.markdown(f"⚖️ 目標まで: **-{weight_diff:.1f}kg**")
                    pace = weight_diff / weeks_left if days_left > 0 else 0
                    if pace > 0.5:
                        st.markdown(f"📉 推奨ペース: 週{pace:.2f}kg ⚠️ ペースが厳しい")
                    else:
                        st.markdown(f"📉 推奨ペース: 週{pace:.2f}kg")
                else:
                    st.markdown(f"⚖️ 目標まで: **+{weight_diff:.1f}kg**")
                    pace = weight_diff / weeks_left if days_left > 0 else 0
                    st.markdown(f"📈 推奨ペース: 週+{pace:.2f}kg")
            else:
                st.caption("大会日を設定すると逆算ペースが表示されます")
        # --- マクロ動的自動調整 ---
        st.markdown("---")
        st.markdown("##### 🔄 マクロ自動調整")
        st.caption("体重と食事の実測データからTDEEを逆算し、目標マクロを自動補正します。")

        # データ収集: 直近14日の体重ログと食事ログ
        adj_weight_log = list(st.session_state.get("_cached_weight_log") or [])
        adj_meals = st.session_state.get("_cached_meals") or {}
        adj_today = now_jst().date()

        # 直近14日の体重データを抽出
        adj_weights = []
        for entry in adj_weight_log:
            try:
                d = datetime.strptime(entry["date"], "%Y-%m-%d").date()
                if (adj_today - d).days <= 14:
                    adj_weights.append({"date": d, "weight": entry["weight"]})
            except (ValueError, KeyError):
                pass
        adj_weights.sort(key=lambda x: x["date"])

        # 直近14日の食事カロリーデータを抽出
        adj_cal_days = []
        for i in range(14):
            d = adj_today - timedelta(days=i)
            d_str = d.strftime("%Y-%m-%d")
            day_data = adj_meals.get(d_str, [])
            if day_data:
                d_cal = sum(m.get("total_cal", 0) for m in day_data)
                adj_cal_days.append({"date": d, "cal": d_cal})

        has_enough_data = len(adj_weights) >= 7 and len(adj_cal_days) >= 7

        if not has_enough_data:
            days_w = len(adj_weights)
            days_m = len(adj_cal_days)
            st.info(f"データ不足 — 体重{days_w}/7日、食事{days_m}/7日。7日以上の記録で自動調整が有効になります。")
        else:
            # 計算: 期間内の平均摂取カロリーと体重変動からTDEEを推定
            avg_intake = sum(d["cal"] for d in adj_cal_days) / len(adj_cal_days)
            first_w = adj_weights[0]["weight"]
            last_w = adj_weights[-1]["weight"]
            weight_change = last_w - first_w  # マイナス=減量
            period_days = (adj_weights[-1]["date"] - adj_weights[0]["date"]).days
            if period_days < 1:
                period_days = 1

            # 体脂肪1kg ≒ 7,700kcal。体重変動からカロリー収支を逆算
            daily_surplus = (weight_change * 7700) / period_days
            estimated_tdee = avg_intake - daily_surplus

            # 現在の目標との差分
            current_target = cached_profile.get("target_cal", 0) if cached_profile else 0
            tdee_diff = estimated_tdee - current_target

            st.markdown(f"**推定TDEE: {estimated_tdee:,.0f} kcal/日**")
            st.caption(f"算出根拠: 平均摂取 {avg_intake:,.0f}kcal × {len(adj_cal_days)}日 / 体重変動 {weight_change:+.1f}kg（{period_days}日間）")

            # 目標タイプに応じた推奨カロリー
            adj_goal_type = cached_profile.get("goal_type", "reduce") if cached_profile else "reduce"
            if adj_goal_type == "bulk":
                recommended_cal = estimated_tdee + 300
                st.markdown(f"- 増量目標: TDEE + 300 = **{recommended_cal:,.0f} kcal/日**")
            else:
                recommended_cal = estimated_tdee - 400
                st.markdown(f"- 減量目標: TDEE - 400 = **{recommended_cal:,.0f} kcal/日**")

            # 現在の目標との比較
            if abs(tdee_diff) > 200:
                if tdee_diff > 0:
                    st.markdown(f"- 現在の目標({current_target:,}kcal)は推定TDEEより **{abs(tdee_diff):.0f}kcal低い** → 想定より速く減量している可能性")
                else:
                    st.markdown(f"- 現在の目標({current_target:,}kcal)は推定TDEEより **{abs(tdee_diff):.0f}kcal高い** → 想定より減量が遅い可能性")
            else:
                st.markdown(f"- 現在の目標({current_target:,}kcal)と推定TDEEは概ね一致。目標設定は適正です。")

            # 推奨PFC計算
            rec_p = prof_weight * 2.0
            rec_f = recommended_cal * 0.25 / 9
            rec_c = (recommended_cal - rec_p * 4 - rec_f * 9) / 4

            st.markdown(f"- 推奨PFC: P:{rec_p:.0f}g / F:{rec_f:.0f}g / C:{rec_c:.0f}g")

            # 適用ボタン
            if st.button("📊 推奨値を目標に適用", key="apply_auto_macro", use_container_width=True):
                updated_profile = dict(cached_profile) if cached_profile else {}
                updated_profile["target_cal"] = round(recommended_cal)
                updated_profile["target_p"] = round(rec_p)
                updated_profile["target_f"] = round(rec_f)
                updated_profile["target_c"] = round(rec_c)
                ls_set("sage_profile", updated_profile)
                st.session_state["_cached_profile"] = updated_profile
                st.success(f"目標を更新しました: {recommended_cal:,.0f}kcal / P:{rec_p:.0f}g F:{rec_f:.0f}g C:{rec_c:.0f}g")
                st.rerun()

    else:
        st.info("💡 プロフィール設定にはBasicプランが必要です")

    st.markdown("---")
    st.markdown("##### 🌿 SAGEとは")
    st.markdown(
        "**SAGE** — ハーブのセージは古代から「万能薬」として重宝され、"
        "その名はラテン語の *salvare*（救う）に由来します。\n\n"
        "英語の *sage* には「賢者」の意味もあります。\n\n"
        "**賢く食べるための道具。** それがSAGEです。"
    )

    st.markdown("##### 📖 使い方ガイド")

    with st.expander("🆓 Free プラン（コード不要）", expanded=not is_standard):
        st.markdown(
            "**できること:** AI食事分析（写真 or テキスト）\n\n"
            "**始め方:**\n"
            "1. 食事モードで写真を撮影 or アップロード\n"
            "2. AIが料理・食材を自動識別\n"
            "3. PFC・カロリーを瞬時に算出\n"
            "4. グラム数を手動で微調整可能\n\n"
            "**テキスト解析:** 「📝 テキスト解析」タブから料理名と材料を入力してもOK\n\n"
            "**手動入力:** PFCを直接入力して記録することも可能"
        )

    with st.expander("🅱️ Basic プラン（¥500/月）", expanded=is_standard and not is_trainee):
        st.markdown(
            "**追加機能:** プロフィール設定 / 目標PFC管理 / 食事履歴保存 / レポート / AIフィードバック\n\n"
            "**セットアップ手順:**\n"
            "1. サイドバーでアクセスコードを入力\n"
            "2. 設定モード → プロフィール設定（体重・目標体重・活動レベル）\n"
            "3. 目標カロリー・PFCを確認（自動計算 or 手動編集）\n"
            "4. 「プロフィールを保存」を押す\n\n"
            "**日々の使い方:**\n"
            "- 食事を記録すると「今日の残り」にカロリー・PFCの残枠が表示される\n"
            "- レポートモードで日次・週次の振り返り\n"
            "- 「💡 FB」タブでAIが食事・トレーニングにフィードバック\n\n"
            "**クラウド同期（推奨）:**\n"
            "- サイドバー「👤 アカウント」で新規登録 → ログイン\n"
            "- データがクラウドにも保存され、機種変・キャッシュクリアでも消えない"
        )

    with st.expander("🅶 Gym プラン（¥660/月）", expanded=is_trainee and not is_stage):
        st.markdown(
            "**追加機能:** トレーニング記録 / 有酸素記録 / セッション消費カロリー / トレ日・オフ日カロリー自動切替\n\n"
            "**筋トレ記録:**\n"
            "1. トレーニングモード →「🏋️ 筋トレ」タブ\n"
            "2. 部位 → 種目 → 重量・回数(Rep)・メモを入力\n"
            "3. 「セット追加」で複数セット記録\n"
            "4. 「記録する」で保存\n\n"
            "**一括インポート:** `ベンチプレス 90×6 90×5 80×10` のように1行1種目で一気に登録\n\n"
            "**有酸素記録:**\n"
            "1. 「🏃 有酸素」タブ\n"
            "2. 種目・時間を入力 → 消費カロリー自動計算\n\n"
            "**セッション時間:**\n"
            "- トレーニング時間と強度を入力 → 推定消費カロリー表示\n\n"
            "**トレ日/オフ日自動切替:**\n"
            "- 設定のプロフィール下部でON → トレ日とオフ日で別々の目標カロリー・PFCを設定可能"
        )

    with st.expander("🅿️ Pro プラン（¥880/月）", expanded=is_stage):
        st.markdown(
            "**追加機能:** 大会Prepモード / 減量・増量モード切替 / Bloom（AIポージング分析）\n\n"
            "**大会Prepモード:**\n"
            "1. 設定で目標タイプ（減量/増量）と大会日を設定\n"
            "2. レポートの「🎯 Prep」タブで大会までのダッシュボード表示\n"
            "3. カウントダウン・体重推移・減量ペース判定・推奨アクションを一画面で確認\n\n"
            "**食事モードでの大会カード:**\n"
            "- 食事記録時に大会までの残り日数・ペースが常に表示される\n\n"
            "**姉妹アプリ:**\n"
            "- 🌸 [ブルーム](https://genkairyoshi-bloom.hf.space) — AIポージング分析（Pro限定）"
        )

    st.markdown("##### 姉妹アプリ")
    st.markdown("🌵 **[カクタス](https://genkairyoshi-cactus.hf.space)** — AI姿勢診断")
    if is_stage:
        st.markdown("🌸 **[ブルーム](https://genkairyoshi-bloom.hf.space)** — AIポージング分析（Pro限定）")

    st.markdown("##### 📱 推奨利用方法")
    st.markdown(
        "**ホーム画面に追加してのご利用を推奨しています。**\n"
        "- iPhone: Safariで開く → 共有ボタン → 「ホーム画面に追加」\n"
        "- Android: Chromeで開く → メニュー → 「ホーム画面に追加」"
    )
    st.markdown("##### 🔒 プライバシー")
    st.markdown(
        "- アップロードされた写真はサーバーに保存されません。分析処理のみに使用されます。\n"
        "- アカウント登録するとデータがクラウドに保存されます。データは暗号化され、本人のみアクセス可能です。\n"
        "- アカウントなしの場合、データはブラウザのみに保存されます。"
    )
    st.markdown("##### 📜 利用規約・プライバシーポリシー")
    st.markdown("[利用規約・プライバシーポリシーはこちら](https://note.com/genkai_ryoshi)")
    st.markdown("##### ⚠️ 注意事項")
    st.markdown(
        "- アカウント未登録の場合、キャッシュクリアやブラウザデータの削除で記録が消える場合があります。\n"
        "- 異なるブラウザや「ホーム画面に追加」ではそれぞれ記録が別々に保存されます。**常に同じ方法でアクセス**してください。\n"
        "- **クラウド同期をONにすると安心です。**"
    )

    # --- データエクスポート ---
    if is_standard:
        st.markdown("---")
        st.markdown("##### 📤 データエクスポート")
        st.caption("記録データをCSVまたはJSON形式でダウンロードできます。")

        export_format = st.radio("形式", ["CSV", "JSON"], horizontal=True, key="export_format")

        if st.button("📥 エクスポート", key="export_btn", use_container_width=True):
            export_meals = st.session_state.get("_cached_meals") or {}
            export_training = st.session_state.get("_cached_training_logs") or []
            export_cardio = st.session_state.get("_cached_cardio_logs") or []
            export_weight = st.session_state.get("_cached_weight_log") or []
            export_profile = st.session_state.get("_cached_profile") or {}

            if export_format == "JSON":
                export_data = {
                    "profile": export_profile,
                    "meals": export_meals,
                    "training_logs": export_training,
                    "cardio_logs": export_cardio,
                    "weight_log": export_weight,
                    "exported_at": now_jst().strftime("%Y-%m-%d %H:%M"),
                }
                json_str = json.dumps(export_data, ensure_ascii=False, indent=2)
                st.download_button(
                    "💾 JSONをダウンロード",
                    data=json_str,
                    file_name=f"sage_export_{now_jst().strftime('%Y%m%d')}.json",
                    mime="application/json",
                    use_container_width=True,
                )
            else:
                # CSV: 食事・トレーニング・有酸素・体重を各シートとしてまとめたCSV
                import io
                csv_parts = []

                # 食事CSV
                csv_parts.append("=== 食事記録 ===")
                csv_parts.append("日付,時刻,料理名,カロリー,P,F,C")
                for date_str, day_meals in sorted(export_meals.items()):
                    if isinstance(day_meals, list):
                        for meal in day_meals:
                            time_str = meal.get("time", "")
                            dishes = meal.get("dishes", [])
                            dish_names = "+".join(d.get("name", "") for d in dishes)
                            cal = meal.get("total_cal", 0)
                            p = meal.get("total_p", 0)
                            f_ = meal.get("total_f", 0)
                            c = meal.get("total_c", 0)
                            csv_parts.append(f"{date_str},{time_str},{dish_names},{cal:.0f},{p:.1f},{f_:.1f},{c:.1f}")

                csv_parts.append("")
                csv_parts.append("=== トレーニング記録 ===")
                csv_parts.append("日付,部位,種目,セット,重量,回数(Rep),メモ")
                for log in export_training:
                    d = log.get("date", "")
                    part = log.get("body_part", "")
                    ex = log.get("exercise", "")
                    for si, s in enumerate(log.get("sets", [])):
                        w = s.get("weight", 0)
                        r = s.get("reps", 0)
                        n = s.get("note", "").replace(",", " ")
                        csv_parts.append(f"{d},{part},{ex},{si+1},{w},{r},{n}")

                csv_parts.append("")
                csv_parts.append("=== 有酸素記録 ===")
                csv_parts.append("日付,種目,時間(分),消費カロリー,メモ")
                for log in export_cardio:
                    d = log.get("date", "")
                    t = log.get("type", "")
                    dur = log.get("duration", 0)
                    cal = log.get("calories", 0)
                    memo = log.get("memo", "").replace(",", " ")
                    csv_parts.append(f"{d},{t},{dur},{cal},{memo}")

                csv_parts.append("")
                csv_parts.append("=== 体重記録 ===")
                csv_parts.append("日付,体重(kg)")
                for entry in export_weight:
                    csv_parts.append(f"{entry.get('date', '')},{entry.get('weight', '')}")

                csv_str = "\n".join(csv_parts)
                st.download_button(
                    "💾 CSVをダウンロード",
                    data=csv_str.encode("utf-8-sig"),
                    file_name=f"sage_export_{now_jst().strftime('%Y%m%d')}.csv",
                    mime="text/csv",
                    use_container_width=True,
                )

    # --- 友達に紹介 ---
    st.markdown("---")
    st.markdown("##### 🔗 友達に紹介する")
    share_url = "https://aomori-fisherman.github.io/genkai-ryoshi/apps/"
    st.code(share_url, language=None)
    try:
        import qrcode
        import io as _io
        qr = qrcode.QRCode(version=1, box_size=8, border=2)
        qr.add_data(share_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="#2c3e50", back_color="white")
        buf = _io.BytesIO()
        qr_img.save(buf, format="PNG")
        st.image(buf.getvalue(), caption="QRコードをスクショして共有", width=200)
    except Exception:
        pass

    # フッター
    st.markdown(
        '<div class="sage-footer">'
        'SAGE by <a href="https://www.instagram.com/genkai_ryoshi/" target="_blank">限界漁師</a><br>'
        '<small>栄養価は推定値です。正確な値は専門家にご相談ください。</small>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.stop()

elif app_mode == "🏋️ トレーニング":
    if not is_trainee:
        if is_standard:
            st.info("💡 トレーニング記録にはGymプラン（¥660/月）へのアップグレードが必要です")
        else:
            st.info("💡 トレーニング記録にはGymプランが必要です")
    else:
        tab_strength, tab_cardio = st.tabs(["🏋️ 筋トレ", "🏃 有酸素"])
        with tab_strength:
            render_training_log()
        with tab_cardio:
            render_cardio_log()
    # フッター
    st.markdown(
        '<div class="sage-footer">'
        'SAGE by <a href="https://www.instagram.com/genkai_ryoshi/" target="_blank">限界漁師</a><br>'
        '<small>栄養価は推定値です。正確な値は専門家にご相談ください。</small>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.stop()

elif app_mode == "📊 レポート":
    # レポートモード（Standard以上）
    if not is_standard:
        st.info("💡 レポート機能にはBasicプラン以上が必要です")
        st.stop()

    import pandas as pd

    # タブ構成: Stage以上なら大会Prepタブを追加
    if is_stage:
        report_tab_daily, report_tab_weekly, report_tab_feedback, report_tab_prep = st.tabs(["📅 日次", "📆 週次", "💡 FB", "🎯 Prep"])
    else:
        report_tab_daily, report_tab_weekly, report_tab_feedback = st.tabs(["📅 日次", "📆 週次", "💡 FB"])

    with report_tab_daily:
        st.markdown("#### 📅 日次レポート")

        # 日付選択（デフォルト: 今日）
        report_date = st.date_input("日付", value=now_jst().date(), key="report_date")
        report_date_str = report_date.strftime("%Y-%m-%d")

        # --- 食事サマリー ---
        st.markdown("##### 🍽️ 食事")
        cur_meals_report = st.session_state.get("_cached_meals") or {}
        day_meals_report = cur_meals_report.get(report_date_str, [])

        if day_meals_report:
            day_cal = sum(m.get("total_cal", 0) for m in day_meals_report)
            day_p = sum(m.get("total_p", 0) for m in day_meals_report)
            day_f = sum(m.get("total_f", 0) for m in day_meals_report)
            day_c = sum(m.get("total_c", 0) for m in day_meals_report)

            st.markdown(f"**{len(day_meals_report)}食 / {day_cal:,.0f}kcal**")
            st.markdown(f"P: {day_p:.1f}g / F: {day_f:.1f}g / C: {day_c:.1f}g")

            # PFCバー（プロフィール目標があれば達成率表示）
            profile = st.session_state.get("_cached_profile")
            if profile and profile.get("target_cal"):
                t_cal = profile["target_cal"]
                t_p = profile.get("target_p", 0)
                t_f = profile.get("target_f", 0)
                t_c = profile.get("target_c", 0)

                # トレ日/オフ日自動切替
                if profile.get("auto_switch"):
                    cur_training_rpt = st.session_state.get("_cached_training_logs") or []
                    has_training_rpt = any(log.get("date") == report_date_str for log in cur_training_rpt)
                    if has_training_rpt:
                        tt_rpt = profile.get("target_train", {})
                        t_cal = tt_rpt.get("cal", t_cal)
                        t_p = tt_rpt.get("p", t_p)
                        t_f = tt_rpt.get("f", t_f)
                        t_c = tt_rpt.get("c", t_c)
                    else:
                        rt_rpt = profile.get("target_rest", {})
                        t_cal = rt_rpt.get("cal", t_cal)
                        t_p = rt_rpt.get("p", t_p)
                        t_f = rt_rpt.get("f", t_f)
                        t_c = rt_rpt.get("c", t_c)

                st.markdown("---")
                st.markdown("**目標達成率**")

                # カロリー
                cal_pct = min(day_cal / t_cal, 1.0) if t_cal > 0 else 0
                st.caption(f"カロリー: {day_cal:,.0f} / {t_cal:,}kcal")
                st.progress(cal_pct)

                # P
                p_pct = min(day_p / t_p, 1.0) if t_p > 0 else 0
                st.caption(f"タンパク質: {day_p:.1f} / {t_p:.0f}g")
                st.progress(p_pct)

                # F
                f_pct = min(day_f / t_f, 1.0) if t_f > 0 else 0
                st.caption(f"脂質: {day_f:.1f} / {t_f:.0f}g")
                st.progress(f_pct)

                # C
                c_pct = min(day_c / t_c, 1.0) if t_c > 0 else 0
                st.caption(f"炭水化物: {day_c:.1f} / {t_c:.0f}g")
                st.progress(c_pct)
            else:
                st.caption("💡 設定でプロフィールを保存すると目標達成率が表示されます")

            # 食事内訳
            st.markdown("---")
            st.markdown("**内訳**")
            for mi, meal in enumerate(day_meals_report):
                time_str = meal.get("time", "")
                dishes = meal.get("dishes", [])
                dish_names = "、".join(d.get("name", "") for d in dishes)
                cal = meal.get("total_cal", 0)
                p = meal.get("total_p", 0)
                f_ = meal.get("total_f", 0)
                c = meal.get("total_c", 0)
                st.caption(f"{time_str} {dish_names} — {cal:.0f}kcal P:{p:.1f} F:{f_:.1f} C:{c:.1f}")
        else:
            st.caption("この日の食事記録はありません")

        # --- トレーニングサマリー ---
        st.markdown("---")
        st.markdown("##### 🏋️ トレーニング")
        cur_training_report = st.session_state.get("_cached_training_logs") or []
        day_training = [log for log in cur_training_report if log.get("date") == report_date_str]

        if day_training:
            # 部位一覧
            parts = []
            for log in day_training:
                p = log.get("body_part", "")
                if p and p not in parts:
                    parts.append(p)

            total_exercises = len(day_training)
            total_sets = sum(len(log.get("sets", [])) for log in day_training)
            total_volume = sum(
                s.get("weight", 0) * s.get("reps", 0)
                for log in day_training
                for s in log.get("sets", [])
            )

            st.markdown(f"**{' / '.join(parts)}**")
            st.markdown(f"{total_exercises}種目 / {total_sets}set / 総挙上重量 {total_volume:,.0f}kg")
            cur_sessions_report = st.session_state.get("_cached_training_sessions") or {}
            session_report = cur_sessions_report.get(report_date_str, {})
            if session_report:
                st.markdown(f"⏱ {session_report.get('duration', 0)}分 / 約{session_report.get('calories', 0)}kcal消費")

            # 種目内訳
            st.markdown("---")
            st.markdown("**内訳**")
            current_part = None
            for log in day_training:
                part = log.get("body_part", "")
                exercise = log.get("exercise", "")
                sets = log.get("sets", [])
                log_idx = cur_training_report.index(log)
                if part != current_part:
                    if current_part is not None:
                        st.markdown("")
                    st.markdown(f"**【{part}】**")
                    current_part = part

                st.markdown(f"　{exercise}")
                for s in sets:
                    w = s.get("weight", 0)
                    r = s.get("reps", 0)
                    note = s.get("note", "")
                    note_text = f"  {note}" if note else ""
                    w_str = f"{w:g}kg" if w > 0 else "自重"
                    st.caption(f"　　{w_str} × {r}{note_text}")

                add_key = f"rpt_add_{log_idx}"
                if st.session_state.get(add_key):
                    col_aw, col_ar = st.columns(2)
                    with col_aw:
                        add_w = st.number_input("重量", min_value=0.0, value=None, step=0.25, format="%.2f",
                                                key=f"rpt_aw_{log_idx}", placeholder="kg", label_visibility="collapsed")
                    with col_ar:
                        add_r = st.number_input("回数(Rep)", min_value=0, value=None, step=1,
                                                key=f"rpt_ar_{log_idx}", placeholder="回数(Rep)", label_visibility="collapsed")
                    add_n = st.text_input("メモ", key=f"rpt_an_{log_idx}", placeholder="メモ（任意）", label_visibility="collapsed")
                    col_save, col_cancel = st.columns(2)
                    with col_save:
                        if st.button("💾 追加", key=f"rpt_as_{log_idx}", use_container_width=True):
                            if (add_w and add_w > 0) or (add_r and add_r > 0):
                                cur_training_report[log_idx]["sets"].append({
                                    "weight": add_w or 0.0, "reps": add_r or 0, "note": add_n or ""
                                })
                                ls_set("sage_training_logs", cur_training_report)
                                st.session_state["_cached_training_logs"] = cur_training_report
                                st.session_state.pop(add_key, None)
                                st.rerun()
                    with col_cancel:
                        if st.button("✕", key=f"rpt_ac_{log_idx}", use_container_width=True):
                            st.session_state.pop(add_key, None)
                            st.rerun()
                else:
                    if st.button(f"➕ セット追加", key=f"rpt_ab_{log_idx}"):
                        st.session_state[add_key] = True
                        st.rerun()
        else:
            st.caption("この日のトレーニング記録はありません")

        # --- 有酸素サマリー ---
        st.markdown("---")
        st.markdown("##### 🏃 有酸素")
        cur_cardio_report = st.session_state.get("_cached_cardio_logs") or []
        day_cardio = [log for log in cur_cardio_report if log.get("date") == report_date_str]

        if day_cardio:
            total_cardio_min = sum(l.get("duration", 0) for l in day_cardio)
            total_cardio_cal = sum(l.get("calories", 0) for l in day_cardio)
            st.markdown(f"**{total_cardio_min}分 / {total_cardio_cal}kcal消費**")
            for log in day_cardio:
                st.caption(f"　{log.get('type', '')} — {log.get('duration', 0)}分 / {log.get('calories', 0)}kcal")
        else:
            st.caption("この日の有酸素記録はありません")

        # --- 体重 ---
        st.markdown("---")
        st.markdown("##### ⚖️ 体重")
        cur_weight_log_daily = st.session_state.get("_cached_weight_log") or []
        day_weight_entry = next((w for w in cur_weight_log_daily if w.get("date") == report_date_str), None)
        if day_weight_entry:
            st.markdown(f"**{day_weight_entry['weight']:.1f} kg**")
        else:
            st.caption("この日の体重記録はありません")

    with report_tab_weekly:
        st.markdown("#### 📆 週次レポート")

        # 基準日（今日）から過去7日分
        today_date = now_jst().date()
        week_start = today_date - timedelta(days=6)
        st.markdown(f"**{week_start.strftime('%m/%d')} 〜 {today_date.strftime('%m/%d')}**")

        # --- 食事週次 ---
        st.markdown("##### 🍽️ 食事（7日間）")
        cur_meals_weekly = st.session_state.get("_cached_meals") or {}

        weekly_cal = 0
        weekly_p = 0
        weekly_f = 0
        weekly_c = 0
        days_with_meals = 0

        for i in range(7):
            d = (week_start + timedelta(days=i)).strftime("%Y-%m-%d")
            day_data = cur_meals_weekly.get(d, [])
            if day_data:
                days_with_meals += 1
                weekly_cal += sum(m.get("total_cal", 0) for m in day_data)
                weekly_p += sum(m.get("total_p", 0) for m in day_data)
                weekly_f += sum(m.get("total_f", 0) for m in day_data)
                weekly_c += sum(m.get("total_c", 0) for m in day_data)

        if days_with_meals > 0:
            avg_cal = weekly_cal / days_with_meals
            avg_p = weekly_p / days_with_meals
            avg_f = weekly_f / days_with_meals
            avg_c = weekly_c / days_with_meals

            st.markdown(f"記録日数: **{days_with_meals}日** / 7日")
            st.markdown(f"**日平均**: {avg_cal:,.0f}kcal / P:{avg_p:.0f}g F:{avg_f:.0f}g C:{avg_c:.0f}g")
            st.markdown(f"**合計**: {weekly_cal:,.0f}kcal / P:{weekly_p:.0f}g F:{weekly_f:.0f}g C:{weekly_c:.0f}g")

            # 日別カロリー一覧
            st.markdown("---")
            st.markdown("**日別カロリー**")
            for i in range(7):
                d_date = week_start + timedelta(days=i)
                d_str = d_date.strftime("%Y-%m-%d")
                d_label = f"{d_date.month}/{d_date.day}"
                day_data = cur_meals_weekly.get(d_str, [])
                if day_data:
                    d_cal = sum(m.get("total_cal", 0) for m in day_data)
                    d_p = sum(m.get("total_p", 0) for m in day_data)
                    st.caption(f"{d_label}: {d_cal:,.0f}kcal (P:{d_p:.0f}g)")
                else:
                    st.caption(f"{d_label}: —")
        else:
            st.caption("この7日間の食事記録はありません")

        # --- トレーニング週次 ---
        st.markdown("---")
        st.markdown("##### 🏋️ トレーニング（7日間）")
        cur_training_weekly = st.session_state.get("_cached_training_logs") or []

        weekly_training = {}
        for i in range(7):
            d = (week_start + timedelta(days=i)).strftime("%Y-%m-%d")
            day_logs = [l for l in cur_training_weekly if l.get("date") == d]
            if day_logs:
                weekly_training[d] = day_logs

        if weekly_training:
            total_sessions = len(weekly_training)
            total_exercises_w = sum(len(logs) for logs in weekly_training.values())
            total_sets_w = sum(
                len(log.get("sets", []))
                for logs in weekly_training.values()
                for log in logs
            )
            total_volume_w = sum(
                s.get("weight", 0) * s.get("reps", 0)
                for logs in weekly_training.values()
                for log in logs
                for s in log.get("sets", [])
            )

            # 部位別集計
            parts_count = {}
            for logs in weekly_training.values():
                for log in logs:
                    p = log.get("body_part", "")
                    if p:
                        parts_count[p] = parts_count.get(p, 0) + 1

            st.markdown(f"トレーニング日数: **{total_sessions}日** / 7日")
            st.markdown(f"**合計**: {total_exercises_w}種目 / {total_sets_w}set / {total_volume_w:,.0f}kg")

            if parts_count:
                parts_summary = " / ".join(f"{p}({c}種目)" for p, c in parts_count.items())
                st.markdown(f"**部位**: {parts_summary}")

            # 日別一覧
            st.markdown("---")
            st.markdown("**日別トレーニング**")
            for i in range(7):
                d_date = week_start + timedelta(days=i)
                d_str = d_date.strftime("%Y-%m-%d")
                d_label = f"{d_date.month}/{d_date.day}"
                if d_str in weekly_training:
                    day_logs = weekly_training[d_str]
                    parts = []
                    for log in day_logs:
                        bp = log.get("body_part", "")
                        if bp and bp not in parts:
                            parts.append(bp)
                    d_sets = sum(len(log.get("sets", [])) for log in day_logs)
                    d_vol = sum(s.get("weight", 0) * s.get("reps", 0) for log in day_logs for s in log.get("sets", []))
                    st.caption(f"{d_label}: {'・'.join(parts)} — {len(day_logs)}種目 / {d_sets}set / {d_vol:,.0f}kg")
                else:
                    st.caption(f"{d_label}: OFF")
        else:
            st.caption("この7日間のトレーニング記録はありません")

        # --- 有酸素週次 ---
        st.markdown("---")
        st.markdown("##### 🏃 有酸素（7日間）")
        cur_cardio_weekly = st.session_state.get("_cached_cardio_logs") or []

        weekly_cardio = {}
        for i in range(7):
            d = (week_start + timedelta(days=i)).strftime("%Y-%m-%d")
            day_logs = [l for l in cur_cardio_weekly if l.get("date") == d]
            if day_logs:
                weekly_cardio[d] = day_logs

        if weekly_cardio:
            total_cardio_sessions = len(weekly_cardio)
            total_cardio_min_w = sum(l.get("duration", 0) for logs in weekly_cardio.values() for l in logs)
            total_cardio_cal_w = sum(l.get("calories", 0) for logs in weekly_cardio.values() for l in logs)

            st.markdown(f"実施日数: **{total_cardio_sessions}日** / 7日")
            st.markdown(f"**合計**: {total_cardio_min_w}分 / {total_cardio_cal_w}kcal消費")
        else:
            st.caption("この7日間の有酸素記録はありません")

        # --- 体重推移（14日間） ---
        st.markdown("---")
        st.markdown("##### ⚖️ 体重推移（14日間）")
        cur_weight_log_weekly = st.session_state.get("_cached_weight_log") or []

        if cur_weight_log_weekly:
            # 直近14日分のデータをフィルタ
            fourteen_days_ago = today_date - timedelta(days=13)
            weight_entries = [
                w for w in cur_weight_log_weekly
                if w.get("date") and w.get("date") >= fourteen_days_ago.strftime("%Y-%m-%d")
            ]
            weight_entries.sort(key=lambda x: x.get("date", ""))

            if weight_entries:
                # チャート表示
                chart_data = pd.DataFrame(weight_entries)
                chart_data["date"] = pd.to_datetime(chart_data["date"])
                chart_data = chart_data.set_index("date")
                st.line_chart(chart_data["weight"])

                # 現在体重
                latest_weight = weight_entries[-1]["weight"]
                st.markdown(f"**現在**: {latest_weight:.1f} kg")

                # 7日移動平均
                if len(weight_entries) >= 2:
                    recent_7 = weight_entries[-7:] if len(weight_entries) >= 7 else weight_entries
                    avg_7 = sum(w["weight"] for w in recent_7) / len(recent_7)
                    st.markdown(f"**7日平均**: {avg_7:.1f} kg")

                    # 週間変化
                    if len(weight_entries) >= 7:
                        older_entries = weight_entries[-14:-7] if len(weight_entries) >= 14 else weight_entries[:len(weight_entries)//2]
                        if older_entries:
                            prev_avg = sum(w["weight"] for w in older_entries) / len(older_entries)
                            weekly_change = avg_7 - prev_avg
                            sign = "+" if weekly_change > 0 else ""
                            st.markdown(f"**週間変化**: {sign}{weekly_change:.2f} kg")
            else:
                st.caption("直近14日間の体重記録はありません")
        else:
            st.caption("体重記録はありません（設定でプロフィールを保存すると記録されます）")

    # --- 大会Prepタブ（Stage/Pro限定） ---
    if is_stage:
        with report_tab_prep:
            st.markdown("#### 🎯 大会Prep")

            profile_prep = st.session_state.get("_cached_profile")
            if not profile_prep or not profile_prep.get("competition_date"):
                st.info("💡 設定で大会日を設定してくださ��")
            else:
                try:
                    comp_date_prep = datetime.strptime(profile_prep["competition_date"], "%Y-%m-%d").date()
                    today_prep = now_jst().date()
                    days_remaining_prep = (comp_date_prep - today_prep).days

                    # カウントダウン
                    st.markdown(f"### 📅 大会まで **{days_remaining_prep}日**")
                    st.caption(f"大会日: {comp_date_prep.strftime('%Y-%m-%d')}")

                    # 体重推移 + 目標ライン
                    st.markdown("---")
                    st.markdown("##### ⚖️ 体重推移")
                    cur_weight_log_prep = st.session_state.get("_cached_weight_log") or []

                    if cur_weight_log_prep:
                        weight_entries_prep = sorted(cur_weight_log_prep, key=lambda x: x.get("date", ""))
                        goal_w = profile_prep.get("goal_weight", 65.0)

                        # チャート: 実測 + 目標ライン
                        chart_df = pd.DataFrame(weight_entries_prep)
                        chart_df["date"] = pd.to_datetime(chart_df["date"])
                        chart_df = chart_df.set_index("date")
                        chart_df["目標"] = goal_w
                        chart_df = chart_df.rename(columns={"weight": "体重"})
                        st.line_chart(chart_df[["体重", "目標"]])

                        # 減量ペース判定
                        st.markdown("---")
                        st.markdown("##### 📉 減量ペース判定")

                        if len(weight_entries_prep) >= 7:
                            # 直近7日と前7日の平均を比較して週間ペースを算出
                            recent_7_prep = weight_entries_prep[-7:]
                            recent_avg = sum(w["weight"] for w in recent_7_prep) / len(recent_7_prep)

                            older_prep = weight_entries_prep[-14:-7] if len(weight_entries_prep) >= 14 else weight_entries_prep[:max(1, len(weight_entries_prep)-7)]
                            older_avg = sum(w["weight"] for w in older_prep) / len(older_prep)

                            weekly_loss = older_avg - recent_avg  # 正なら減量中

                            if weekly_loss > 0.5:
                                st.warning(f"週間変動: -{weekly_loss:.2f}kg — ペース速い（筋量低下リスク）")
                            elif weekly_loss < 0.1:
                                st.warning(f"週間変動: -{weekly_loss:.2f}kg — 停滞気味")
                            else:
                                st.success(f"週間変動: -{weekly_loss:.2f}kg — 適正ペース")
                        else:
                            st.caption("7日以上の記録でペース判定が表示されます")
                    else:
                        st.caption("体重記録��ありません")

                    # 週間PFC平均
                    st.markdown("---")
                    st.markdown("##### 🍽️ 週間PFC平均")
                    cur_meals_prep = st.session_state.get("_cached_meals") or {}
                    week_start_prep = today_prep - timedelta(days=6)

                    prep_cal = 0
                    prep_p = 0
                    prep_f = 0
                    prep_c = 0
                    prep_days = 0

                    for i in range(7):
                        d = (week_start_prep + timedelta(days=i)).strftime("%Y-%m-%d")
                        day_data = cur_meals_prep.get(d, [])
                        if day_data:
                            prep_days += 1
                            prep_cal += sum(m.get("total_cal", 0) for m in day_data)
                            prep_p += sum(m.get("total_p", 0) for m in day_data)
                            prep_f += sum(m.get("total_f", 0) for m in day_data)
                            prep_c += sum(m.get("total_c", 0) for m in day_data)

                    if prep_days > 0:
                        st.markdown(f"記録日数: **{prep_days}日** / 7日")
                        st.markdown(f"- カロリー: **{prep_cal / prep_days:,.0f}** kcal/日")
                        st.markdown(f"- P: **{prep_p / prep_days:.0f}** g/日")
                        st.markdown(f"- F: **{prep_f / prep_days:.0f}** g/日")
                        st.markdown(f"- C: **{prep_c / prep_days:.0f}** g/日")
                    else:
                        st.caption("この7日間の食事記録はありません")

                    # 推奨アクション
                    st.markdown("---")
                    st.markdown("##### 💡 推奨アクション")
                    if cur_weight_log_prep and len(cur_weight_log_prep) >= 7:
                        current_w = cur_weight_log_prep[-1]["weight"] if cur_weight_log_prep else profile_prep.get("weight", 70)
                        remaining_to_lose = current_w - goal_w

                        if days_remaining_prep <= 0:
                            st.markdown("- 大会当日。最高のコンディションを。")
                        elif remaining_to_lose <= 0:
                            st.markdown("- 目標体重到達済み。体重維持+コンディション調整へ。")
                        else:
                            needed_pace = remaining_to_lose / max(days_remaining_prep / 7, 0.1)
                            if needed_pace > 0.7:
                                st.markdown(f"- 残り{remaining_to_lose:.1f}kgを{days_remaining_prep}日で落とすには週{needed_pace:.2f}kg。ペース厳しい — カーディオ増か摂取見直しを。")
                            elif needed_pace > 0.4:
                                st.markdown(f"- 残り{remaining_to_lose:.1f}kg / 週{needed_pace:.2f}kgペースが必要。現状維持で到達可能圏内。")
                            else:
                                st.markdown(f"- 残り{remaining_to_lose:.1f}kg / 余裕あり。急ぎすぎず筋量キープ優先で。")
                    else:
                        st.caption("7日以上の体重記録で推奨アクションが表示されます")

                except (ValueError, TypeError):
                    st.error("大会日の形式が不正です。設定で修正してください。")

    with report_tab_feedback:
        st.markdown("#### 💡 AIフィードバック")
        st.caption("直近7日間のデータをAIが分析し、食事・トレーニングへのフィードバックを提供します。")

        # Collect data for analysis
        profile_fb = st.session_state.get("_cached_profile")
        if not profile_fb:
            st.info("プロフィールを設定すると、パーソナライズされたフィードバックが表示されます。")
        else:
            # Gather 7-day meal data
            cur_meals_fb = st.session_state.get("_cached_meals") or {}
            today_fb = now_jst().date()
            week_start_fb = today_fb - timedelta(days=6)

            meal_summary_lines = []
            total_days_with_meals = 0
            weekly_cal_fb = 0
            weekly_p_fb = 0
            weekly_f_fb = 0
            weekly_c_fb = 0
            for i in range(7):
                d = (week_start_fb + timedelta(days=i)).strftime("%Y-%m-%d")
                day_data = cur_meals_fb.get(d, [])
                if day_data:
                    total_days_with_meals += 1
                    d_cal = sum(m.get("total_cal", 0) for m in day_data)
                    d_p = sum(m.get("total_p", 0) for m in day_data)
                    d_f = sum(m.get("total_f", 0) for m in day_data)
                    d_c = sum(m.get("total_c", 0) for m in day_data)
                    weekly_cal_fb += d_cal
                    weekly_p_fb += d_p
                    weekly_f_fb += d_f
                    weekly_c_fb += d_c
                    meal_summary_lines.append(f"{d}: {d_cal:.0f}kcal P:{d_p:.0f}g F:{d_f:.0f}g C:{d_c:.0f}g")

            # Gather 7-day training data
            cur_training_fb = st.session_state.get("_cached_training_logs") or []
            training_summary_lines = []
            training_days_fb = 0
            for i in range(7):
                d = (week_start_fb + timedelta(days=i)).strftime("%Y-%m-%d")
                day_logs = [l for l in cur_training_fb if l.get("date") == d]
                if day_logs:
                    training_days_fb += 1
                    parts = []
                    for log in day_logs:
                        bp = log.get("body_part", "")
                        if bp and bp not in parts:
                            parts.append(bp)
                    d_sets = sum(len(log.get("sets", [])) for log in day_logs)
                    d_vol = sum(s.get("weight", 0) * s.get("reps", 0) for log in day_logs for s in log.get("sets", []))
                    training_summary_lines.append(f"{d}: {'・'.join(parts)} {len(day_logs)}種目 {d_sets}set {d_vol:,.0f}kg")

            # Gather weight data
            cur_weight_fb = st.session_state.get("_cached_weight_log") or []
            weight_lines = []
            for entry in cur_weight_fb:
                weight_lines.append(f"{entry.get('date')}: {entry.get('weight')}kg")

            # Gather cardio data
            cur_cardio_fb = st.session_state.get("_cached_cardio_logs") or []
            cardio_summary_lines = []
            for i in range(7):
                d = (week_start_fb + timedelta(days=i)).strftime("%Y-%m-%d")
                day_logs = [l for l in cur_cardio_fb if l.get("date") == d]
                if day_logs:
                    total_min = sum(l.get("duration", 0) for l in day_logs)
                    total_cal_c = sum(l.get("calories", 0) for l in day_logs)
                    cardio_summary_lines.append(f"{d}: {total_min}分 {total_cal_c}kcal")

            if total_days_with_meals == 0 and training_days_fb == 0:
                st.info("直近7日間のデータがありません。食事やトレーニングを記録してからフィードバックを受けましょう。")
            else:
                # Build prompt
                target_cal = profile_fb.get("target_cal", 0)
                target_p = profile_fb.get("target_p", 0)
                target_f = profile_fb.get("target_f", 0)
                target_c = profile_fb.get("target_c", 0)
                current_weight = profile_fb.get("weight", 70)
                goal_weight = profile_fb.get("goal_weight", 65)
                goal_type = profile_fb.get("goal_type", "reduce")

                # Competition info
                comp_date = profile_fb.get("competition_date", "")
                comp_info = ""
                if comp_date:
                    try:
                        comp_dt = datetime.strptime(comp_date, "%Y-%m-%d").date()
                        days_left = (comp_dt - today_fb).days
                        comp_info = f"大会日: {comp_date}（残り{days_left}日）"
                    except ValueError:
                        pass

                feedback_prompt = f"""あなたはボディビル・フィジーク競技者向けの栄養・トレーニングコーチです。
以下のデータを分析し、具体的で実践的なフィードバックを日本語で提供してください。

## ユーザー情報
- 現在体重: {current_weight}kg
- 目標体重: {goal_weight}kg
- 目標タイプ: {"減量" if goal_type == "reduce" else "増量"}
- 目標カロリー: {target_cal}kcal/日
- 目標PFC: P:{target_p}g F:{target_f}g C:{target_c}g
{f"- {comp_info}" if comp_info else ""}

## 直近7日間の食事記録（{total_days_with_meals}日分）
{chr(10).join(meal_summary_lines) if meal_summary_lines else "記録なし"}

## 直近7日間のトレーニング記録（{training_days_fb}日分）
{chr(10).join(training_summary_lines) if training_summary_lines else "記録なし"}

## 有酸素記録
{chr(10).join(cardio_summary_lines) if cardio_summary_lines else "記録なし"}

## 体重推移
{chr(10).join(weight_lines[-14:]) if weight_lines else "記録なし"}

## 回答フォーマット（厳守）
以下の4セクションで回答してください。各セクション2-3行で簡潔に。

**食事の評価**
（カロリー・PFCバランスの達成度、改善点）

**トレーニングの評価**
（ボリューム・頻度・部位バランスの評価）

**良い点**
（継続できていること、数値から見える成果）

**改善アクション**
（今週取り組むべき具体的なアクション1-2個）
"""

                if st.button("🔍 AIフィードバックを受ける", key="feedback_btn", use_container_width=True):
                    with st.spinner("データを分析中..."):
                        try:
                            response = model.generate_content(feedback_prompt)
                            feedback_text = response.text.strip()
                            st.session_state["_last_feedback"] = feedback_text
                            st.session_state["_last_feedback_date"] = today_fb.strftime("%Y-%m-%d")
                        except Exception as e:
                            st.error(f"フィードバック生成に失敗しました: {e}")

                # Display cached feedback
                if st.session_state.get("_last_feedback"):
                    fb_date = st.session_state.get("_last_feedback_date", "")
                    st.caption(f"最終分析: {fb_date}")
                    st.markdown(st.session_state["_last_feedback"])

    # フッター
    st.markdown(
        '<div class="sage-footer">'
        'SAGE by <a href="https://www.instagram.com/genkai_ryoshi/" target="_blank">限界漁師</a><br>'
        '<small>栄養価は推定値です。正確な値は専門家にご相談ください。</small>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.stop()

# --- 食事モード ---
st.markdown("#### 🍽️ 食事を記録")
tab_photo, tab_text, tab_manual, tab_bulk = st.tabs(["📸 写真で記録", "📝 テキスト解析", "✏️ 手動入力", "📋 一括インポート"])

with tab_photo:
    uploaded_file = st.file_uploader(
        "写真を選択",
        type=["jpg", "jpeg", "png", "webp"],
        key="uploader",
    )

with tab_text:
    st.markdown("料理名と材料を入力すると、AIが栄養成分を推定します。")
    text_dish_name = st.text_input("料理名", key="text_dish_name", placeholder="例: 鶏むね肉のソテー")
    text_ingredients = st.text_area(
        "材料・量",
        key="text_ingredients",
        placeholder="例: 鶏むね肉200g, ブロッコリー100g, オリーブオイル5g",
        height=100,
    )
    tcol1, tcol2 = st.columns(2)
    with tcol1:
        text_analyze_btn = st.button("🔍 解析する", key="text_analyze_btn", use_container_width=True)
    with tcol2:
        if st.button("🗑 クリア", key="text_clear_btn", use_container_width=True):
            for k in ["text_dish_name", "text_ingredients", "analysis_result", "original_data", "adjusted_grams", "confirmed_grams", "meal_saved"]:
                if k in st.session_state:
                    del st.session_state[k]
            st.rerun()

with tab_manual:
    st.markdown("PFCを直接入力して記録します。")
    manual_dish_name = st.text_input("料理名", key="manual_dish_name", placeholder="例: プロテインシェイク")
    manual_time = st.text_input("時刻", key="manual_time", placeholder=f"例: {now_jst().strftime('%H:%M')}（空欄で現在時刻）")
    mcol1, mcol2 = st.columns(2)
    with mcol1:
        manual_cal = st.number_input("カロリー (kcal)", min_value=0, value=None, step=1, key="manual_cal", placeholder="0")
    with mcol2:
        manual_p = st.number_input("P (g)", min_value=0.0, value=None, step=0.1, format="%.1f", key="manual_p", placeholder="0.0")
    mcol3, mcol4 = st.columns(2)
    with mcol3:
        manual_f = st.number_input("F (g)", min_value=0.0, value=None, step=0.1, format="%.1f", key="manual_f", placeholder="0.0")
    with mcol4:
        manual_c = st.number_input("C (g)", min_value=0.0, value=None, step=0.1, format="%.1f", key="manual_c", placeholder="0.0")
    mbtncol1, mbtncol2 = st.columns(2)
    with mbtncol1:
        manual_save_btn = st.button("💾 記録する", key="manual_save_btn", use_container_width=True)
    with mbtncol2:
        if st.button("🗑 クリア", key="manual_clear_btn", use_container_width=True):
            for k in ["manual_dish_name", "manual_cal", "manual_p", "manual_f", "manual_c"]:
                if k in st.session_state:
                    del st.session_state[k]
            st.rerun()

with tab_bulk:
    st.markdown("1行ずつ `時刻 料理名 カロリー P F C` の形式で入力してください。")
    st.caption("例: 12:00 鶏ハム 220 49.3 3.8 0.0")
    bulk_text = st.text_area("一括入力", key="bulk_text", height=200, placeholder="12:00 鶏ハム 220 49.3 3.8 0.0\n18:30 プロテイン 120 24.0 1.5 3.0")
    bulk_import_btn = st.button("📋 一括登録", key="bulk_import_btn", use_container_width=True)

# ============================================================
# 一括インポートの保存処理
# ============================================================
if bulk_import_btn and bulk_text:
    if not is_standard:
        st.info("💡 食事の記録にはBasicプランが必要です。")
    else:
        lines = [l.strip() for l in bulk_text.strip().split("\n") if l.strip()]
        imported = 0
        errors = []
        all_meals = dict(cached_meals) if isinstance(cached_meals, dict) else {}
        today_key = now_jst().strftime("%Y-%m-%d")
        if today_key not in all_meals:
            all_meals[today_key] = []

        for li, line in enumerate(lines):
            try:
                # 時刻部分を抽出（HH:MM形式）
                parts = line.split()
                if len(parts) < 6:
                    errors.append(f"行{li+1}: 項目不足 → {line}")
                    continue
                time_str = parts[0]
                # 最後の4つが数値（cal, P, F, C）
                c_val = float(parts[-1])
                f_val = float(parts[-2])
                p_val = float(parts[-3])
                cal_val = float(parts[-4])
                # 間が料理名
                dish_name = " ".join(parts[1:-4])

                meal_entry = {
                    "time": time_str,
                    "dishes": [{"name": dish_name, "grams": 0, "cal": cal_val, "protein": p_val, "fat": f_val, "carb": c_val, "ingredients": []}],
                    "total_cal": cal_val,
                    "total_p": p_val,
                    "total_f": f_val,
                    "total_c": c_val,
                }
                all_meals[today_key].append(meal_entry)
                imported += 1
            except (ValueError, IndexError):
                errors.append(f"行{li+1}: パース失敗 → {line}")

        if imported > 0:
            ls_set("sage_meals", all_meals)
            st.session_state["_cached_meals"] = all_meals
            st.success(f"✅ {imported}食を登録しました")
        if errors:
            for e in errors:
                st.warning(e)

# ============================================================
# 手動入力の保存処理
# ============================================================
if manual_save_btn:
    if not manual_dish_name:
        st.error("料理名を入力してください。")
    elif not is_standard:
        st.info("💡 食事の記録にはBasicプランが必要です。サイドバーからアクセスコードを入力してください。")
    else:
        now = now_jst()
        today_key = now.strftime("%Y-%m-%d")
        time_str = manual_time.strip() if manual_time and manual_time.strip() else now.strftime("%H:%M")

        meal_entry = {
            "time": time_str,
            "dishes": [{"name": manual_dish_name, "grams": 0, "cal": manual_cal or 0, "protein": manual_p or 0.0, "fat": manual_f or 0.0, "carb": manual_c or 0.0, "ingredients": []}],
            "total_cal": manual_cal or 0,
            "total_p": manual_p or 0.0,
            "total_f": manual_f or 0.0,
            "total_c": manual_c or 0.0,
        }

        all_meals = dict(cached_meals) if isinstance(cached_meals, dict) else {}
        if today_key not in all_meals:
            all_meals[today_key] = []
        all_meals[today_key].append(meal_entry)

        ls_set("sage_meals", all_meals)
        st.session_state["_cached_meals"] = all_meals
        st.success(f"✅ {manual_dish_name} を記録しました（{manual_cal or 0}kcal P:{manual_p or 0.0}g F:{manual_f or 0.0}g C:{manual_c or 0.0}g）")

# ============================================================
# テキスト解析の処理
# ============================================================
if text_analyze_btn:
    if not text_dish_name or not text_ingredients:
        st.error("料理名と材料・量を入力してください。")
    else:
        # レートリミットチェック（写真解析と共有）
        if "analysis_timestamps" not in st.session_state:
            st.session_state.analysis_timestamps = []
        now = now_jst()
        st.session_state.analysis_timestamps = [
            t for t in st.session_state.analysis_timestamps if now - t < RATE_LIMIT_WINDOW
        ]
        if len(st.session_state.analysis_timestamps) >= RATE_LIMIT_MAX:
            st.error("分析回数の上限に達しました。しばらく時間をおいてから再度お試しください。")
        else:
            text_prompt = f"""以下の料理の栄養成分を分析してください。

料理名: {text_dish_name}
材料・量: {text_ingredients}

以下のJSON形式で回答してください（JSON以外のテキストは含めないでください）:
{{
  "dishes": [
    {{
      "name": "料理名",
      "ingredients": [
        {{"name": "食材名", "gram": グラム数, "calorie": カロリー, "protein": タンパク質g, "fat": 脂質g, "carb": 炭水化物g}}
      ]
    }}
  ]
}}

注意:
- 数値は小数点第1位まで
- 日本語で料理名・食材名を記載してください
"""
            with st.spinner("テキストから栄養成分を解析中..."):
                try:
                    response = model.generate_content(text_prompt)
                    text_resp = response.text.strip()
                    text_resp = re.sub(r"^```(?:json)?\s*", "", text_resp)
                    text_resp = re.sub(r"\s*```$", "", text_resp)
                    text_result = json.loads(text_resp)

                    if "error" in text_result:
                        st.warning(f"⚠️ {text_result['error']}")
                    elif "dishes" in text_result and len(text_result["dishes"]) > 0:
                        st.session_state.analysis_timestamps.append(now_jst())
                        # 写真解析と同じ結果形式にセット
                        st.session_state.analysis_result = text_result
                        st.session_state.original_data = json.loads(json.dumps(text_result))
                        grams = {}
                        for di, dish in enumerate(text_result.get("dishes", [])):
                            for ii, ing in enumerate(dish.get("ingredients", [])):
                                key = f"{di}_{ii}"
                                grams[key] = float(ing.get("gram", 0))
                        st.session_state.adjusted_grams = grams
                        st.session_state.confirmed_grams = dict(grams)
                        st.session_state.edit_version = 0
                        st.session_state.meal_saved = False
                        st.session_state.last_image_hash = None  # 写真モードのキャッシュをクリア
                        st.rerun()
                    else:
                        st.error("解析結果を取得できませんでした。入力内容を確認してください。")
                except json.JSONDecodeError:
                    st.error("AIの応答を解析できませんでした。もう一度お試しください。")
                except Exception as e:
                    st.error(f"解析中にエラーが発生しました: {e}")

# ============================================================
# 写真解析の処理
# ============================================================
# 画像の取得
image_source = uploaded_file
if image_source is None:
    # 写真が消されたら分析結果もクリア
    if st.session_state.get("last_image_hash") is not None:
        for k in ["last_image_hash", "analysis_result", "original_data", "adjusted_grams", "confirmed_grams", "meal_saved"]:
            st.session_state.pop(k, None)
        st.session_state.edit_version = st.session_state.get("edit_version", 0) + 1
        stale_keys = [k for k in st.session_state if k.startswith(("dish_name_", "name_", "gram_"))]
        for k in stale_keys:
            del st.session_state[k]
    # 写真がなくても、テキスト解析の結果がある場合は結果表示に進む
    if st.session_state.get("analysis_result") is None:
        st.info("📸 写真を撮影、テキストで解析、または手動で入力して食事を記録できます。")
        # 食事履歴（結果なし時も表示）
        st.markdown("---")
        if is_standard:
            st.markdown("**📅 食事履歴（直近7日）**")
            cur_cached_meals_early = st.session_state.get("_cached_meals") or {}
            if cur_cached_meals_early:
                today_str_early = now_jst().strftime("%Y-%m-%d")
                sorted_dates_early = sorted(cur_cached_meals_early.keys(), reverse=True)[:7]
                if sorted_dates_early:
                    for date_str in sorted_dates_early:
                        day_meals = cur_cached_meals_early[date_str]
                        if not isinstance(day_meals, list):
                            continue
                        try:
                            dt = datetime.strptime(date_str, "%Y-%m-%d")
                            month_day = f"{dt.month}/{dt.day}"
                        except ValueError:
                            month_day = date_str
                        day_label = "今日" if date_str == today_str_early else ""
                        meal_count = len(day_meals)
                        day_cal = sum(m.get("total_cal", 0) for m in day_meals)
                        day_p = sum(m.get("total_p", 0) for m in day_meals)
                        header = f"{month_day}{'（' + day_label + '）' if day_label else ''}　{meal_count}食 {day_cal:,.0f}kcal P:{day_p:.0f}g"
                        is_today_early = date_str == today_str_early

                        with st.expander(header, expanded=is_today_early):
                            for mi, meal in enumerate(day_meals):
                                time_str = meal.get("time", "")
                                dishes = meal.get("dishes", [])
                                dish_names = "、".join(d.get("name", "") for d in dishes)
                                cal = meal.get("total_cal", 0)
                                p = meal.get("total_p", 0)
                                f_ = meal.get("total_f", 0)
                                c = meal.get("total_c", 0)
                                st.markdown(
                                    f"**{time_str}** {dish_names}  \n"
                                    f"{cal:.0f}kcal P:{p:.1f}g F:{f_:.1f}g C:{c:.1f}g"
                                )
                                if st.button(f"🗑 削除", key=f"del_meal_early_{date_str}_{mi}"):
                                    cur_cached_meals_early[date_str].pop(mi)
                                    if not cur_cached_meals_early[date_str]:
                                        del cur_cached_meals_early[date_str]
                                    ls_set("sage_meals", cur_cached_meals_early)
                                    st.session_state["_cached_meals"] = cur_cached_meals_early
                                    st.rerun()
                else:
                    st.caption("まだ記録がありません")
            else:
                st.caption("まだ記録がありません")
        else:
            st.info("💡 食事履歴にはBasicプランが必要です")
        # フッター（結果なし時も表示）
        st.markdown(
            '<div class="sage-footer">'
            'SAGE by <a href="https://www.instagram.com/genkai_ryoshi/" target="_blank">限界漁師</a><br>'
            '<small>栄養価は推定値です。正確な値は専門家にご相談ください。</small>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()
else:
    # 画像表示 & リサイズ（API高速化のため）
    image = Image.open(image_source)
    image = ImageOps.exif_transpose(image)  # EXIF回転を適用（iPhone対策）
    MAX_SIZE = 1024
    if max(image.size) > MAX_SIZE:
        image.thumbnail((MAX_SIZE, MAX_SIZE), Image.LANCZOS)
    st.image(image, caption="アップロードされた写真", use_container_width=True)

    # ============================================================
    # AI分析（写真）
    # ============================================================

    # セッションステートで分析結果を管理
    # 画像が変わったら再分析するためにハッシュをキーにする
    image_bytes = image_source.getvalue()
    image_hash = hash(image_bytes)

    if "last_image_hash" not in st.session_state or st.session_state.last_image_hash != image_hash:
        st.session_state.last_image_hash = image_hash
        st.session_state.analysis_result = None
        st.session_state.original_data = None
        st.session_state.adjusted_grams = {}
        st.session_state.meal_saved = False
        st.session_state.edit_version = st.session_state.get("edit_version", 0) + 1
        st.session_state.confirmed_grams = {}
        # ウィジェットキャッシュを全削除（前回の料理名・食材名が残る問題の対策）
        stale_keys = [k for k in st.session_state if k.startswith(("dish_name_", "name_", "gram_"))]
        for k in stale_keys:
            del st.session_state[k]

    if st.session_state.analysis_result is None:
        # レートリミットチェック
        if "analysis_timestamps" not in st.session_state:
            st.session_state.analysis_timestamps = []
        now = now_jst()
        st.session_state.analysis_timestamps = [
            t for t in st.session_state.analysis_timestamps if now - t < RATE_LIMIT_WINDOW
        ]
        if len(st.session_state.analysis_timestamps) >= RATE_LIMIT_MAX:
            st.error("分析回数の上限に達しました。しばらく時間をおいてから再度お試しください。")
            st.stop()

        st.info("通常10〜15秒で完了します")
        with st.spinner("分析中... 写真から料理と食材を読み取っています"):
            result = analyze_meal_image(model, image)
            if result is None:
                st.error("分析に失敗しました。別の角度で撮影し直してみてください。")
                st.stop()
            if "error" in result:
                st.warning(f"⚠️ {result['error']}")
                st.stop()
            st.session_state.analysis_timestamps.append(now_jst())
            st.session_state.analysis_result = result
            # オリジナルデータを保持（比例計算用）
            st.session_state.original_data = json.loads(json.dumps(result))
            # 初期g数を設定
            grams = {}
            for di, dish in enumerate(result.get("dishes", [])):
                for ii, ing in enumerate(dish.get("ingredients", [])):
                    key = f"{di}_{ii}"
                    grams[key] = float(ing.get("gram", 0))
            st.session_state.adjusted_grams = grams
        st.session_state.confirmed_grams = dict(grams)
        st.session_state.edit_version = 0
        st.session_state.meal_saved = False

if "confirmed_grams" not in st.session_state:
    st.session_state.confirmed_grams = dict(st.session_state.adjusted_grams)
if "edit_version" not in st.session_state:
    st.session_state.edit_version = 0
if "meal_saved" not in st.session_state:
    st.session_state.meal_saved = False

ev = st.session_state.edit_version

result = st.session_state.analysis_result
original = st.session_state.original_data

if not result or "dishes" not in result or len(result["dishes"]) == 0:
    st.warning("料理を識別できませんでした。別の写真をお試しください。")
    st.stop()

# ============================================================
# 分析結果セクション見出し
# ============================================================
st.markdown("---")
st.markdown("#### 🍽️ 分析結果")
st.caption("料理をタップで展開。グラム数を変更して「再計算」ボタンで確定。")

# ============================================================
# 計算パス（表示なし）: 確定済みg数で合計を先に算出
# ============================================================
use_grams = st.session_state.confirmed_grams

total_cal = 0.0
total_p = 0.0
total_f = 0.0
total_c = 0.0

for di, dish in enumerate(result.get("dishes", [])):
    ingredients = dish.get("ingredients", [])
    for ii, ing in enumerate(ingredients):
        key = f"{di}_{ii}"
        orig_ing = original["dishes"][di]["ingredients"][ii]
        orig_gram = float(orig_ing.get("gram", 1)) or 1
        cur_gram = float(use_grams.get(key, orig_gram))
        ratio = cur_gram / orig_gram if orig_gram > 0 else 0
        total_cal += float(orig_ing.get("calorie", 0)) * ratio
        total_p += float(orig_ing.get("protein", 0)) * ratio
        total_f += float(orig_ing.get("fat", 0)) * ratio
        total_c += float(orig_ing.get("carb", 0)) * ratio

# ============================================================
# 合計表示（食材リストより先に出す）
# ============================================================
st.markdown(
    f'<div class="total-card">'
    f'<h2>1食トータル</h2>'
    f'<div class="cal-big">{total_cal:.0f}</div>'
    f'<div class="cal-unit">kcal</div>'
    f'</div>',
    unsafe_allow_html=True,
)

st.markdown(
    f'<div class="pfc-row">'
    f'<div class="pfc-item pfc-p">'
    f'<div class="label">タンパク質</div>'
    f'<div class="value">{total_p:.1f}</div>'
    f'<div class="unit">g</div>'
    f'</div>'
    f'<div class="pfc-item pfc-f">'
    f'<div class="label">脂質</div>'
    f'<div class="value">{total_f:.1f}</div>'
    f'<div class="unit">g</div>'
    f'</div>'
    f'<div class="pfc-item pfc-c">'
    f'<div class="label">炭水化物</div>'
    f'<div class="value">{total_c:.1f}</div>'
    f'<div class="unit">g</div>'
    f'</div>'
    f'</div>',
    unsafe_allow_html=True,
)

# PFCバランスバー
total_pfc_g = total_p + total_f + total_c
if total_pfc_g > 0:
    p_pct = total_p / total_pfc_g * 100
    f_pct = total_f / total_pfc_g * 100
    c_pct = total_c / total_pfc_g * 100
    st.markdown(
        f"""
        <div style="display:flex; height:12px; border-radius:6px; overflow:hidden; margin:0.5rem 0 1rem 0;">
            <div style="width:{p_pct}%; background:#3498db;" title="P {p_pct:.0f}%"></div>
            <div style="width:{f_pct}%; background:#e67e22;" title="F {f_pct:.0f}%"></div>
            <div style="width:{c_pct}%; background:#f1c40f;" title="C {c_pct:.0f}%"></div>
        </div>
        <div style="display:flex; justify-content:space-between; font-size:0.75rem; color:#95a5a6;">
            <span>🟦 P {p_pct:.0f}%</span>
            <span>🟧 F {f_pct:.0f}%</span>
            <span>🟨 C {c_pct:.0f}%</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ============================================================
# 今日の残り枠表示
# ============================================================
if profile_saved:
    saved_prof = st.session_state["_cached_profile"]
    t_cal = saved_prof.get("target_cal", 0)
    t_p = saved_prof.get("target_p", 0)
    t_f = saved_prof.get("target_f", 0)
    t_c = saved_prof.get("target_c", 0)

    # トレ日/オフ日自動切替
    if saved_prof.get("auto_switch"):
        today_str_check = now_jst().strftime("%Y-%m-%d")
        cur_training_today = st.session_state.get("_cached_training_logs") or []
        has_training_today = any(log.get("date") == today_str_check for log in cur_training_today)
        if has_training_today:
            tt = saved_prof.get("target_train", {})
            t_cal = tt.get("cal", t_cal)
            t_p = tt.get("p", t_p)
            t_f = tt.get("f", t_f)
            t_c = tt.get("c", t_c)
        else:
            rt = saved_prof.get("target_rest", {})
            t_cal = rt.get("cal", t_cal)
            t_p = rt.get("p", t_p)
            t_f = rt.get("f", t_f)
            t_c = rt.get("c", t_c)

    # 今日の履歴合算
    today_str = now_jst().strftime("%Y-%m-%d")
    today_meals = cached_meals.get(today_str, []) if isinstance(cached_meals, dict) else []
    eaten_cal = sum(m.get("total_cal", 0) for m in today_meals)
    eaten_p = sum(m.get("total_p", 0) for m in today_meals)
    eaten_f = sum(m.get("total_f", 0) for m in today_meals)
    eaten_c = sum(m.get("total_c", 0) for m in today_meals)

    # 現在の分析結果も含める
    used_cal = eaten_cal + total_cal
    used_p = eaten_p + total_p
    used_f = eaten_f + total_f
    used_c = eaten_c + total_c

    remain_cal = t_cal - used_cal
    remain_p = t_p - used_p
    remain_f = t_f - used_f
    remain_c = t_c - used_c

    if remain_cal >= 0:
        cal_class = ""
        cal_text = f"残り {remain_cal:.0f}"
    else:
        cal_class = " over"
        cal_text = f"超過 {abs(remain_cal):.0f}"

    # PFC残り表示（マイナスなら赤）
    def fmt_remain(val, label):
        if val >= 0:
            return f"{label}: {val:.0f}g"
        else:
            return f'<span style="color:#e74c3c">{label}: {val:.0f}g</span>'

    pfc_html = " / ".join([
        fmt_remain(remain_p, "P"),
        fmt_remain(remain_f, "F"),
        fmt_remain(remain_c, "C"),
    ])

    st.markdown(
        f'<div class="remaining-card">'
        f'<h2>今日の残り</h2>'
        f'<div class="remaining-cal{cal_class}">{cal_text}</div>'
        f'<div class="remaining-unit">kcal</div>'
        f'<div class="remaining-pfc">{pfc_html}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Stage: 大会カウントダウンカード
    if is_stage and saved_prof.get("competition_date"):
        try:
            comp_date = datetime.strptime(saved_prof["competition_date"], "%Y-%m-%d").date()
            today_date = now_jst().date()
            days_remaining = (comp_date - today_date).days
            current_weight = saved_prof.get("weight", 70.0)
            goal_weight = saved_prof.get("goal_weight", 65.0)
            saved_goal_type = saved_prof.get("goal_type", "reduce")

            if saved_goal_type == "bulk":
                w_diff = goal_weight - current_weight
                weeks_rem = max(days_remaining / 7, 0.1)
                weekly_pace = w_diff / weeks_rem if days_remaining > 0 else 0
                detail_text = f"目標まで +{w_diff:.1f}kg | 週+{weekly_pace:.2f}kgペース"
            else:
                w_diff = current_weight - goal_weight
                weeks_rem = max(days_remaining / 7, 0.1)
                weekly_pace = w_diff / weeks_rem if days_remaining > 0 else 0
                detail_text = f"目標まで -{w_diff:.1f}kg | 週{weekly_pace:.2f}kgペース"

            st.markdown(
                f'<div class="stage-card">'
                f'<h2>🎯 大会まで {days_remaining}日</h2>'
                f'<div class="stage-detail">{detail_text}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        except (ValueError, TypeError):
            pass

# ============================================================
# 食材リスト表示 & g数修正（expander）
# ============================================================
for di, dish in enumerate(result.get("dishes", [])):
    dish_name = dish.get("name", "不明な料理")
    ingredients = dish.get("ingredients", [])

    # 料理ごとの小計（確定済みg数で計算）
    dish_cal = 0.0
    dish_p = 0.0
    dish_f = 0.0
    dish_c = 0.0
    for ii, ing in enumerate(ingredients):
        key = f"{di}_{ii}"
        orig_ing = original["dishes"][di]["ingredients"][ii]
        orig_gram = float(orig_ing.get("gram", 1)) or 1
        cur_gram = float(use_grams.get(key, orig_gram))
        ratio = cur_gram / orig_gram if orig_gram > 0 else 0
        dish_cal += float(orig_ing.get("calorie", 0)) * ratio
        dish_p += float(orig_ing.get("protein", 0)) * ratio
        dish_f += float(orig_ing.get("fat", 0)) * ratio
        dish_c += float(orig_ing.get("carb", 0)) * ratio

    # 料理名の編集
    edited_dish_name = st.text_input(
        "料理名",
        value=dish_name,
        key=f"dish_name_{di}_v{ev}",
        label_visibility="collapsed",
    )
    if edited_dish_name != dish_name:
        result["dishes"][di]["name"] = edited_dish_name
        dish_name = edited_dish_name

    # 折りたたみで料理ごとに表示
    with st.expander(
        f"🍳 {dish_name}　—　{dish_cal:.0f}kcal　P{dish_p:.1f}g　F{dish_f:.1f}g　C{dish_c:.1f}g",
        expanded=False,
    ):
        # 削除対象を追跡
        to_delete = None

        for ii, ing in enumerate(ingredients):
            key = f"{di}_{ii}"
            orig_ing = original["dishes"][di]["ingredients"][ii]
            orig_gram = float(orig_ing.get("gram", 1)) or 1

            ing_name = ing.get("name", "不明")

            # 食材名（編集可能）+ g数 + 削除ボタン
            col_name, col_gram, col_del = st.columns([3, 2, 0.5])
            with col_name:
                edited_name = st.text_input(
                    "食材名",
                    value=ing_name,
                    key=f"name_{key}_v{ev}",
                    label_visibility="collapsed",
                )
                # 名前が変わったら反映
                if edited_name != ing_name:
                    result["dishes"][di]["ingredients"][ii]["name"] = edited_name
            with col_gram:
                new_gram = st.number_input(
                    f"{ing_name} (g)",
                    min_value=0.0,
                    value=float(st.session_state.adjusted_grams.get(key, orig_gram)),
                    step=1.0,
                    key=f"gram_{key}_v{ev}",
                    label_visibility="collapsed",
                )
                st.session_state.adjusted_grams[key] = new_gram
            with col_del:
                if st.button("✕", key=f"del_{key}_v{ev}", help="この食材を削除"):
                    to_delete = ii

            # 確定済みg数で表示
            confirmed_gram = float(use_grams.get(key, orig_gram))
            ratio = confirmed_gram / orig_gram if orig_gram > 0 else 0
            cal = float(orig_ing.get("calorie", 0)) * ratio
            protein = float(orig_ing.get("protein", 0)) * ratio
            fat = float(orig_ing.get("fat", 0)) * ratio
            carb = float(orig_ing.get("carb", 0)) * ratio

            # 食材ごとのPFC（色分けカード）
            st.markdown(
                f'<div class="ing-row"><div class="ing-vals">'
                f'<div class="ing-val ing-val-cal">{cal:.0f}kcal</div>'
                f'<div class="ing-val ing-val-p">P {protein:.1f}g</div>'
                f'<div class="ing-val ing-val-f">F {fat:.1f}g</div>'
                f'<div class="ing-val ing-val-c">C {carb:.1f}g</div>'
                f'</div></div>',
                unsafe_allow_html=True,
            )

        # 削除処理
        if to_delete is not None:
            del_key = f"{di}_{to_delete}"
            result["dishes"][di]["ingredients"].pop(to_delete)
            original["dishes"][di]["ingredients"].pop(to_delete)
            # キーの再割り当て
            new_grams = {}
            new_confirmed = {}
            for ii2 in range(len(result["dishes"][di]["ingredients"])):
                old_k = f"{di}_{ii2 if ii2 < to_delete else ii2 + 1}"
                new_k = f"{di}_{ii2}"
                new_grams[new_k] = st.session_state.adjusted_grams.get(old_k, 0)
                new_confirmed[new_k] = st.session_state.confirmed_grams.get(old_k, 0)
            # 他の料理のキーを保持
            for k, v in st.session_state.adjusted_grams.items():
                if not k.startswith(f"{di}_"):
                    new_grams[k] = v
            for k, v in st.session_state.confirmed_grams.items():
                if not k.startswith(f"{di}_"):
                    new_confirmed[k] = v
            st.session_state.adjusted_grams = new_grams
            st.session_state.confirmed_grams = new_confirmed
            st.session_state.analysis_result = result
            st.session_state.original_data = original
            st.session_state.edit_version += 1
            st.rerun()

        st.markdown("---")

        # 食材追加
        st.markdown("**食材を追加**")
        add_col1, add_col2 = st.columns([3, 2])
        with add_col1:
            new_ing_name = st.text_input("食材名", key=f"add_name_{di}_v{ev}", placeholder="例: 卵")
        with add_col2:
            new_ing_gram = st.number_input("g数", min_value=0.0, value=100.0, step=1.0, key=f"add_gram_{di}_v{ev}")

        if st.button("➕ 追加", key=f"add_btn_{di}_v{ev}"):
            if new_ing_name:
                with st.spinner("PFCを推定中..."):
                    pfc = estimate_ingredient_pfc(model, new_ing_name, new_ing_gram)
                if pfc:
                    new_ii = len(result["dishes"][di]["ingredients"])
                    new_key = f"{di}_{new_ii}"
                    result["dishes"][di]["ingredients"].append(pfc)
                    original["dishes"][di]["ingredients"].append(json.loads(json.dumps(pfc)))
                    st.session_state.adjusted_grams[new_key] = float(pfc.get("gram", new_ing_gram))
                    st.session_state.confirmed_grams[new_key] = float(pfc.get("gram", new_ing_gram))
                    st.session_state.analysis_result = result
                    st.session_state.original_data = original
                    st.session_state.edit_version += 1
                    st.rerun()
                else:
                    st.error("PFCの推定に失敗しました。")

        # 再計算ボタン
        if st.button("🔄 再計算", key=f"recalc_{di}_v{ev}"):
            for ii2 in range(len(result["dishes"][di]["ingredients"])):
                k = f"{di}_{ii2}"
                st.session_state.confirmed_grams[k] = st.session_state.adjusted_grams[k]
            st.rerun()

# ============================================================
# テキストコピー
# ============================================================
st.markdown("---")
# meal-log用のテキストを生成
copy_lines = []
for di, dish in enumerate(result.get("dishes", [])):
    dish_name = dish.get("name", "")
    ing_parts = []
    for ii, ing in enumerate(dish.get("ingredients", [])):
        key = f"{di}_{ii}"
        g = st.session_state.confirmed_grams.get(key, float(ing.get("gram", 0)))
        ing_parts.append(f"{ing.get('name', '')}{g:.0f}g")
    copy_lines.append(f"{dish_name} {' '.join(ing_parts)}")

copy_text = " / ".join(copy_lines) + f"\nP{total_p:.1f}g F{total_f:.1f}g C{total_c:.1f}g {total_cal:.0f}kcal"

st.text_area("📋 コピー用テキスト", value=copy_text, height=80, label_visibility="collapsed")
st.caption("↑ タップして全選択→コピー → meal-logにそのまま貼れます")

# ============================================================
# 食事記録保存ボタン（Basic限定）
# ============================================================
if not is_standard:
    st.info("💡 食事の記録・履歴管理にはBasicプランが必要です。サイドバーからアクセスコードを入力してください。")
elif st.session_state.meal_saved:
    st.success("✅ 記録済み")
else:
    save_time_input = st.text_input("時刻", key="save_meal_time", placeholder=f"例: {now_jst().strftime('%H:%M')}（空欄で現在時刻）")
    if st.button("💾 記録する", key="save_meal", use_container_width=True):
        # 保存データ構築
        now = now_jst()
        today_key = now.strftime("%Y-%m-%d")
        time_str = save_time_input.strip() if save_time_input and save_time_input.strip() else now.strftime("%H:%M")

        dishes_for_save = []
        for di, dish in enumerate(result.get("dishes", [])):
            dish_ings = []
            for ii, ing in enumerate(dish.get("ingredients", [])):
                key = f"{di}_{ii}"
                orig_ing = original["dishes"][di]["ingredients"][ii]
                orig_gram = float(orig_ing.get("gram", 1)) or 1
                cur_gram = float(use_grams.get(key, orig_gram))
                ratio = cur_gram / orig_gram if orig_gram > 0 else 0
                dish_ings.append({
                    "name": ing.get("name", ""),
                    "gram": cur_gram,
                    "calorie": round(float(orig_ing.get("calorie", 0)) * ratio, 1),
                    "protein": round(float(orig_ing.get("protein", 0)) * ratio, 1),
                    "fat": round(float(orig_ing.get("fat", 0)) * ratio, 1),
                    "carb": round(float(orig_ing.get("carb", 0)) * ratio, 1),
                })
            dishes_for_save.append({
                "name": dish.get("name", ""),
                "ingredients": dish_ings,
            })

        meal_entry = {
            "time": time_str,
            "dishes": dishes_for_save,
            "total_cal": round(total_cal, 1),
            "total_p": round(total_p, 1),
            "total_f": round(total_f, 1),
            "total_c": round(total_c, 1),
        }

        # 既存データに追記
        all_meals = dict(cached_meals) if isinstance(cached_meals, dict) else {}
        if today_key not in all_meals:
            all_meals[today_key] = []
        all_meals[today_key].append(meal_entry)

        # localStorage保存
        ls_set("sage_meals", all_meals)
        st.session_state["_cached_meals"] = all_meals
        st.session_state.meal_saved = True
        st.rerun()

# ============================================================
# 食事履歴（メインエリア・Standard以上）
# ============================================================
st.markdown("---")
if is_standard:
    st.markdown("**📅 食事履歴（直近7日）**")
    cur_cached_meals = st.session_state.get("_cached_meals") or {}
    if cur_cached_meals:
        today_str_hist = now_jst().strftime("%Y-%m-%d")
        sorted_dates_hist = sorted(cur_cached_meals.keys(), reverse=True)[:7]
        if sorted_dates_hist:
            for date_str in sorted_dates_hist:
                day_meals = cur_cached_meals[date_str]
                if not isinstance(day_meals, list):
                    continue
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                    month_day = f"{dt.month}/{dt.day}"
                except ValueError:
                    month_day = date_str
                day_label = "今日" if date_str == today_str_hist else ""
                meal_count = len(day_meals)
                day_cal = sum(m.get("total_cal", 0) for m in day_meals)
                day_p = sum(m.get("total_p", 0) for m in day_meals)
                header = f"{month_day}{'（' + day_label + '）' if day_label else ''}　{meal_count}食 {day_cal:,.0f}kcal P:{day_p:.0f}g"
                is_today_hist = date_str == today_str_hist

                with st.expander(header, expanded=is_today_hist):
                    for mi, meal in enumerate(day_meals):
                        time_str = meal.get("time", "")
                        dishes = meal.get("dishes", [])
                        dish_names = "、".join(d.get("name", "") for d in dishes)
                        cal = meal.get("total_cal", 0)
                        p = meal.get("total_p", 0)
                        f_ = meal.get("total_f", 0)
                        c = meal.get("total_c", 0)
                        st.markdown(
                            f"**{time_str}** {dish_names}  \n"
                            f"{cal:.0f}kcal P:{p:.1f}g F:{f_:.1f}g C:{c:.1f}g"
                        )
                        if st.button(f"🗑 削除", key=f"del_meal_{date_str}_{mi}"):
                            cur_cached_meals[date_str].pop(mi)
                            if not cur_cached_meals[date_str]:
                                del cur_cached_meals[date_str]
                            ls_set("sage_meals", cur_cached_meals)
                            st.session_state["_cached_meals"] = cur_cached_meals
                            st.rerun()
        else:
            st.caption("まだ記録がありません")
    else:
        st.caption("まだ記録がありません")
else:
    st.info("💡 食事履歴にはBasicプランが必要です")

# ============================================================
# フッター
# ============================================================
st.markdown(
    '<div class="sage-footer">'
    'SAGE by <a href="https://www.instagram.com/genkai_ryoshi/" target="_blank">限界漁師</a><br>'
    '<small>栄養価は推定値です。正確な値は専門家にご相談ください。</small>'
    '</div>',
    unsafe_allow_html=True,
)

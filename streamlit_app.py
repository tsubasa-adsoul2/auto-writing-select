# streamlit_app.py
# ------------------------------------------------------------
# WP Auto Writer (Final One‑Shot / 互換・完全版 / 統合ポリシー版 + 共起語対応)
# - ④ポリシーは .txt を「1ファイル=1区分」で保持（中に [リード文]/[本文指示]/[まとめ文] を含める）
#   ※区切りが無い古い .txt は「本文のみ」として互換運用（リード/まとめは既定文を適用）
# - ①読者像 / ②ニーズ / ③構成 をAI生成（H2は最小/最大数を強制遵守）
# - 記事（リード→本文→まとめ）は 1 回のリクエストで一括生成
# - 🚫禁止事項は手入力のみ（アップロードなし）
# - ✅共起語入力（改行/カンマ区切り）→本文へ自然に散りばめる／未出現は警告
# - ポリシープリセット：.txt読み込み→選択→編集→上書き/削除→ローカルキャッシュでF5後も維持
# - ?rest_route= 優先でWP下書き/予約/公開（403回避）
# - カテゴリ選択：Secretsの `wp_configs.<site>.categories` があれば使用 / 無ければRESTで取得
# - 公開状態：日本語UI（下書き/予約投稿/公開）→ API送信値は英語にマップ
# - 本文文字数：最小/最大と“厳密制御（不足/超過 自動調整）”
# ------------------------------------------------------------
from __future__ import annotations

import re
import json
from pathlib import Path
from datetime import datetime, timezone, time as dt_time
from typing import Dict, Any, List, Tuple

import requests
from requests.auth import HTTPBasicAuth
import streamlit as st

# ==============================
# 基本設定
# ==============================
st.set_page_config(page_title="WP Auto Writer", page_icon="📝", layout="wide")
st.title("📝 WP Auto Writer")

# ------------------------------
# Secrets 読み込み
# ------------------------------
if "wp_configs" not in st.secrets:
    st.error("Secrets に [wp_configs] がありません。App settings → Secrets で登録してください。")
    st.stop()

WP_CONFIGS: Dict[str, Dict[str, Any]] = st.secrets["wp_configs"]  # 複数サイト対応
GEMINI_KEY = st.secrets.get("google", {}).get("gemini_api_key_1", None)
if not GEMINI_KEY:
    st.warning("Gemini APIキー（google.gemini_api_key_1）が未設定です。生成機能は動作しません。")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (AutoWriter/Streamlit)",
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

# ------------------------------
# WP エンドポイント補助
# ------------------------------
def ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"

def api_candidates(base: str, route: str) -> List[str]:
    base = ensure_trailing_slash(base)
    route = route.lstrip("/")
    # ?rest_route= 優先（WAF回避）
    return [f"{base}?rest_route=/{route}", f"{base}wp-json/{route}"]

def wp_get(base: str, route: str, auth: HTTPBasicAuth, headers: Dict[str, str]) -> requests.Response | None:
    last = None
    for url in api_candidates(base, route):
        r = requests.get(url, auth=auth, headers=headers, timeout=20)
        last = r
        if r.status_code == 200:
            return r
    return last

def wp_post(base: str, route: str, auth: HTTPBasicAuth, headers: Dict[str, str],
            json_payload: Dict[str, Any]) -> requests.Response | None:
    last = None
    for url in api_candidates(base, route):
        r = requests.post(url, auth=auth, headers=headers, json=json_payload, timeout=45)
        last = r
        if r.status_code in (200, 201):
            return r
    return last

# ==== まとめ欠落の自動補完ヘルパー ====
import re

def _has_summary(html: str) -> bool:
    """<h2>タグ内に「まとめ」を含む見出しがあるか判定（大文字小文字無視）"""
    return bool(re.search(r'(?i)<h2>[^<]*まとめ[^<]*</h2>', html or ""))

def _extract_h2_titles(html: str):
    """本文中の <h2> タイトルを配列で返す（HTMLタグ除去、はじめに/まとめ除外）"""
    titles = re.findall(r'(?is)<h2>(.*?)</h2>', html or "")
    clean = [re.sub(r'<.*?>', '', t).strip() for t in titles]
    return [t for t in clean if t and t not in ("はじめに", "まとめ")]

def _append_fallback_summary(html: str) -> str:
    """<h2>まとめ</h2> が無いときに、ローカルで汎用のまとめを末尾に付与（LLM不使用＝追加料金ゼロ）"""
    heads = _extract_h2_titles(html)[:3]
    bullets = "".join([f"<li>{h}の要点を確認しましょう。</li>" for h in heads]) or "<li>本記事の要点を振り返りましょう。</li>"
    fallback = (
        "\n<h2>まとめ</h2>\n"
        "<p>本記事のポイントを簡潔に整理します。</p>\n"
        f"<ul>{bullets}</ul>\n"
        "<p>詳細は各セクションを参照し、実践へつなげてください。</p>\n"
    )
    return (html.rstrip() + "\n\n" + fallback)


# ------------------------------
# 生成ユーティリティ / バリデータ
# ------------------------------
ALLOWED_TAGS = ['h2', 'h3', 'p', 'strong', 'em', 'ul', 'ol', 'li', 'table', 'tr', 'th', 'td']  # <br>禁止
MAX_H2 = 8
H2_RE = re.compile(r'(<h2>.*?</h2>)', re.IGNORECASE | re.DOTALL)

def simplify_html(html: str) -> str:
    # 許可タグ以外を除去 + <br>禁止
    tags = re.findall(r'</?(\w+)[^>]*>', html)
    for tag in set(tags):
        if tag.lower() not in ALLOWED_TAGS:
            html = re.sub(rf'</?{tag}[^>]*>', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<br\s*/?>', '', html, flags=re.IGNORECASE)
    return html

def validate_article(html: str) -> List[str]:
    warns: List[str] = []
    if re.search(r'<h4|<script|<style', html, flags=re.IGNORECASE):
        warns.append("禁止タグ（h4/script/style）が含まれています。")
    if re.search(r'<br\s*/?>', html, flags=re.IGNORECASE):
        warns.append("<br> タグは使用禁止です。すべて <p> に置き換えてください。")
    # H2ごとに表or箇条書き
    h2_iter = list(re.finditer(r'(<h2>.*?</h2>)', html, flags=re.DOTALL | re.IGNORECASE))
    for i, m in enumerate(h2_iter):
        start = m.end()
        end = h2_iter[i + 1].start() if i + 1 < len(h2_iter) else len(html)
        section = html[start:end]
        if not re.search(r'<(ul|ol|table)\b', section, flags=re.IGNORECASE):
            warns.append("H2セクションに表（table）または箇条書き（ul/ol）が不足しています。")
    # h3直下の<p>分量
    h3_positions = list(re.finditer(r'(<h3>.*?</h3>)', html, flags=re.DOTALL | re.IGNORECASE))
    for i, m in enumerate(h3_positions):
        start = m.end()
        next_head = re.search(r'(<h2>|<h3>)', html[start:], flags=re.IGNORECASE)
        end = start + next_head.start() if next_head else len(html)
        block = html[start:end]
        p_count = len(re.findall(r'<p>.*?</p>', block, flags=re.DOTALL | re.IGNORECASE))
        if p_count < 3 or p_count > 6:
            warns.append("各<h3>直下は4〜5文（<p>）が目安です。分量を調整してください。")
    # 全文ざっくり長さ
    plain = re.sub(r'<.*?>', '', html)
    if len(plain.strip()) > 6000:
        warns.append("記事全体が6000文字を超えています。要約・整理してください。")
    return warns

def count_h2(html: str) -> int:
    return len(H2_RE.findall(html or ""))

def trim_h2_max(structure_html: str, max_count: int) -> str:
    parts = H2_RE.split(structure_html)
    out: List[str] = []
    h2_seen = 0
    i = 0
    while i < len(parts):
        chunk = parts[i]
        if H2_RE.match(chunk or ""):
            h2_seen += 1
            if h2_seen <= max_count:
                out.append(chunk)
                if i + 1 < len(parts):
                    out.append(parts[i + 1])
            i += 2
            continue
        else:
            if h2_seen == 0:
                out.append(chunk)
            i += 1
    return "".join(out)

def strip_existing_summary_h2(structure_html: str) -> str:
    """構成③中に紛れた「まとめ」系H2をすべて除去（構成は本文用だけにする）"""
    # 「まとめ」を含む<h2>～直後の<h3>群を丸ごと消す（次の<h2>直前まで）
    out = []
    i = 0
    pattern_h2 = re.compile(r'(?is)<h2>(.*?)</h2>')
    matches = list(pattern_h2.finditer(structure_html))
    last_end = 0
    for idx, m in enumerate(matches):
        title = re.sub(r'<.*?>', '', m.group(1) or '').strip()
        next_start = matches[idx + 1].start() if idx + 1 < len(matches) else len(structure_html)
        block = structure_html[m.start():next_start]
        if "まとめ" in title:
            # スキップ（入れない）
            pass
        else:
            # 直前のテキスト片を保持
            if last_end < m.start():
                out.append(structure_html[last_end:m.start()])
            out.append(block)
        last_end = next_start
    if last_end < len(structure_html):
        out.append(structure_html[last_end:])
    return "".join(out).strip()

def enforce_summary_last(structure_html: str, keyword: str, total_h2: int) -> str:
    """
    総H2数 total_h2 のうち最後の1つを必ず
    <h2>{keyword}に関するまとめ</h2>
    に固定する。③構成は本文用H2だけ（= total_h2-1 個）にそろえる。
    """
    # まず③構成内に紛れた「まとめ」H2は全部削除（本文用に純化）
    structure_html = strip_existing_summary_h2(structure_html)

    # 本文用の上限は total_h2 - 1
    content_max = max(total_h2 - 1, 0)
    if count_h2(structure_html) > content_max:
        structure_html = trim_h2_max(structure_html, content_max)

    # 最後に「まとめ」H2を強制付与
    summary_h2 = f"\n<h2>{keyword}に関するまとめ</h2>\n"
    return (structure_html.rstrip() + summary_h2)


# ------------------------------
# 本文文字数制御（必要なら再利用）
# ------------------------------

import re

def _summary_span(html: str) -> tuple[int, int] | None:
    """<h2>まとめ</h2> セクションの [開始, 終了) インデックスを返す。無ければ None。"""
    m = re.search(r'(?i)<h2>\s*まとめ\s*</h2>', html)
    if not m:
        return None
    start = m.start()
    # 次の<h2> までが まとめセクション
    m2 = re.search(r'(?i)<h2>', html[m.end():])
    end = m.end() + (m2.start() if m2 else 0)
    return (start, end if m2 else len(html))

def _visible_len(s: str) -> int:
    return len(re.sub(r'<.*?>', '', s or '', flags=re.DOTALL).strip())

def _trim_by_p(html_block: str, limit: int) -> str:
    """<p>単位で前から積み上げて limit 以内に収める（タグは壊さない素朴版）。"""
    parts = re.findall(r'(?si).*?(?:<p>.*?</p>|$)', html_block)
    out = ""
    for part in parts:
        cand = out + part
        if _visible_len(cand) <= limit:
            out = cand
        else:
            break
    return out if out else html_block[:limit]

def cap_summary(html: str, limit_chars: int = 320) -> str:
    """まとめセクションを limit_chars 以内にカット（<p>単位）。"""
    span = _summary_span(html)
    if not span:
        return html
    s, e = span
    head = html[:s]
    body = html[s:e]
    tail = html[e:]
    trimmed = _trim_by_p(body, limit_chars)
    return head + trimmed + tail

# ------------------------------
# 本文文字数制御（必要なら再利用）
# ------------------------------
def visible_length(html: str) -> int:
    text = re.sub(r'<.*?>', '', html or '', flags=re.DOTALL)
    return len(text.strip())

def trim_to_max_chars(html: str, limit: int) -> str:
    if visible_length(html) <= limit:
        return html
    parts = re.findall(r'(?si).*?(?:<p>.*?</p>|$)', html)
    out = ""
    for part in parts:
        if visible_length(out + part) <= limit:
            out += part
        else:
            break
    return out if out else html[:limit]

def prompt_append_chars(keyword: str, co_terms: List[str], current_html: str, need_chars: int) -> str:
    co_block = "\n".join([f"- {w}" for w in co_terms]) if co_terms else "（なし）"
    return f"""
あなたは日本語のSEOライターです。
以下の既存HTML本文に、不足分として**約{need_chars}文字**の<p>段落を追記してください。

# 制約
- 新しい<h2>/<h3>は禁止（構成を増やさない）
- <p>/<ul>/<ol>/<table>のみ使用可
- 既存内容との重複や矛盾を避ける
- 共起語は不自然にならない範囲で可能な限り織り込む
- 1文は55文字以内、<br>禁止
- 出力は追加部分のHTMLのみ（既存本文は再出力しない）

# 主キーワード
{keyword}

# 共起語（任意で自然に反映）
{co_block}

# 既存本文
{current_html}
""".strip()

# ------------------------------
# Gemini 呼び出し
# ------------------------------
def call_gemini(prompt: str, temperature: float = 0.2, model: str = "gemini-1.5-pro") -> str:
    if not GEMINI_KEY:
        raise RuntimeError("Gemini APIキーが未設定です。Secrets に google.gemini_api_key_1 を追加してください。")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": temperature}}
    r = requests.post(endpoint, json=payload, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini エラー: {r.status_code} / {r.text[:500]}")
    j = r.json()
    return j["candidates"][0]["content"]["parts"][0]["text"]

# 既存の関数はそのまま保持（バックアップ用）
def generate_seo_title(keyword: str, content_dir: str) -> str:
    """SEOタイトル生成（バックアップ用）"""
    p = f"""
# 役割: SEO編集者
# 指示: 以下のキーワードから魅力的なSEOタイトルを生成

# 制約:
- 32文字以内
- 日本語のみ
- 【】や｜禁止
- キーワードを自然に含める
- クリックしたくなる魅力的な内容

# 入力:
- キーワード: {keyword}
- 方向性: {content_dir}

# 出力: タイトルのみ
"""
    result = call_gemini(p, model=st.session_state.get("selected_model", "gemini-1.5-pro")).strip()
    # クリーニング
    result = re.sub(r'[【】｜\n\r]', '', result)[:32]
    return result

def generate_seo_description(keyword: str, content_dir: str, title: str) -> str:
    """メタディスクリプション生成（バックアップ用）"""
    p = f"""
# 役割: SEO編集者
# 指示: 以下の情報からメタディスクリプションを生成

# 制約:
- 120字以内
- 定型「〜を解説/紹介」禁止
- 数字や具体メリットを含める
- CTRを高める表現

# 入力:
- キーワード: {keyword}
- タイトル: {title}
- 方向性: {content_dir}

# 出力: 説明文のみ
"""
    result = call_gemini(p).strip()
    # クリーニング
    result = re.sub(r'[\n\r]', '', result)[:120]
    return result


# ------------------------------
# プロンプト群（共起語対応）
# ------------------------------
def prompt_outline_123(keyword: str, extra: str, banned: List[str], co_terms: List[str], min_h2: int, max_h2: int) -> str:
    banned_block = "\n".join([f"・{b}" for b in banned]) if banned else "（なし）"
    co_block = "\n".join([f"・{w}" for w in co_terms]) if co_terms else "（指定なし）"
    return f"""
# 役割
あなたは日本語SEOに強いWeb編集者。キーワードから「①読者像」「②ニーズ」「③構成(HTML)」を作る。④は不要。

# 入力
- キーワード: {keyword}
- 追加要素: {extra or "（指定なし）"}
- 共起語（本文に自然に散りばめる想定 / 出力は③だけでOK）:
{co_block}
- 禁止事項（絶対に含めない）:
{banned_block}

# 制約
- ①/②は150字程度で箇条書き
- ③は <h2>,<h3> のみ（<h1>禁止）
- H2は最低 {min_h2} 個、最大 {max_h2} 個
- 各<h2>の下に<h3>は必ず3つ以上
- H2直下で「この記事では〜」などの定型句は使わない（導入は後工程）

# 出力フォーマット（厳守）
① 読者像:
- ...

② ニーズ:
- ...

③ 構成（HTML）:
<h2>...</h2>
<h3>...</h3>
""".strip()

def prompt_fill_h2(keyword: str, existing_structure_html: str, need: int) -> str:
    return f"""
# 役割: SEO編集者
# 指示: 既存の構成（<h2>,<h3>）に不足があるため、追加のH2ブロックをちょうど {need} 個だけ作る。
# 厳守:
- 出力は追加分のみ。前後の説明や余計な文章は出さない
- 各ブロックは <h2>見出し</h2> の直後に <h3> を3つ以上
- すべて日本語。<h1>は禁止。<br>は禁止

# 既存の構成（参考・重複は避ける）
{existing_structure_html}

# 出力（追加分のみ）
""".strip()

def prompt_full_article_unified(keyword: str,
                                unified_policy_text: str,
                                structure_html: str,
                                readers_txt: str,
                                needs_txt: str,
                                banned: List[str],
                                co_terms: List[str],
                                min_chars: int,
                                max_chars: int) -> str:
    lead_pol, body_pol, summary_pol = extract_sections(unified_policy_text)
    if not lead_pol:
        lead_pol = """# リード文の作成指示:
・読者の悩みや不安を共感的に表現すること
・記事で得られる具体的メリットを2つ以上
・最後に行動を促す一文
"""
    if not summary_pol:
        summary_pol = """# まとめ文の作成指示:
・最初に<h2>{keyword}に関するまとめ</h2>
・要点を2-3個リストで挿入
・約300文字
"""
    lead_pol = lead_pol.replace("{keyword}", keyword)
    body_pol = body_pol.replace("{keyword}", keyword)
    summary_pol = summary_pol.replace("{keyword}", keyword)
    banned_block = "\n".join([f"・{b}" for b in banned]) if banned else "（なし）"
    co_block = "\n".join([f"・{w}" for w in co_terms]) if co_terms else "（任意・無理に詰め込まない）"
    return f"""
# 命令書:
あなたはSEOに特化した日本語のプロライターです。
以下の構成案と各ポリシーに従い、「{keyword}」の記事を
**リード文 → 本文 → まとめ**まで一気通貫でHTMLのみ出力してください。

# 文字数ガイド（本文合計）
・概ね {min_chars}〜{max_chars} 字に収めること

# リード文ポリシー（厳守）
{lead_pol}

# 本文ポリシー（厳守）
{body_pol}

# まとめ文ポリシー（厳守）
{summary_pol}

# 共起語（本文で“自然に”散りばめる・過度に詰め込み禁止）
{co_block}

# 禁止事項（絶対に含めない）
{banned_block}

# 記事の方向性（参考）
[読者像]
{readers_txt}

[ニーズ]
{needs_txt}

# 構成案（この<h2><h3>構成を厳密に守る）
{structure_html}

# 出力
（HTMLのみを出力）
""".strip()

# ------------------------------
# タイトル/説明 & スラッグ
# ------------------------------
def generate_title_and_description_unified(keyword: str, content_dir: str) -> tuple[str, str]:
    """タイトルとメタディスクリプションを1回で生成"""
    p = f"""
# 役割: SEO編集者
# 指示: 以下を同時に生成してください

## 1. SEOタイトル
- 32文字以内
- 日本語のみ
- 【】や｜禁止
- キーワードを自然に含める
- クリックしたくなる魅力的な内容

## 2. メタディスクリプション  
- 120字以内
- 定型「〜を解説/紹介」禁止
- 数字や具体メリットを含める
- CTRを高める表現

# 入力
- キーワード: {keyword}
- 方向性: {content_dir}

# 出力フォーマット（厳守）
タイトル: ここにタイトル
説明: ここに説明文
"""
    result = call_gemini(p).strip()
    
    # 結果をパース
    title_match = re.search(r'タイトル:\s*(.+)', result)
    desc_match = re.search(r'説明:\s*(.+)', result)
    
    title = title_match.group(1).strip() if title_match else f"{keyword}について"
    desc = desc_match.group(1).strip() if desc_match else f"{keyword}に関する情報をお届けします。"
    
    # クリーニング
    title = re.sub(r'[【】｜\n\r]', '', title)[:32]
    desc = re.sub(r'[\n\r]', '', desc)[:120]
    
    return title, desc


def generate_permalink(keyword_or_title: str) -> str:
    import re as _re
    from datetime import datetime as _dt
    try:
        from unidecode import unidecode
        def _jp_to_romaji(s: str) -> str:
            return unidecode(s)
    except Exception:
        try:
            from pykakasi import kakasi
            _kk = kakasi()
            _kk.setMode("J", "a")
            _conv = _kk.getConverter()
            def _jp_to_romaji(s: str) -> str:
                return _conv.do(s)
        except Exception:
            def _jp_to_romaji(s: str) -> str:
                return s
    s = (keyword_or_title or "").strip()
    if not s:
        return f"post-{int(_dt.now().timestamp())}"
    s = _jp_to_romaji(s).lower()
    s = s.replace("&", " and ").replace("+", " plus ")
    s = _re.sub(r"[^a-z0-9\s-]", "", s)
    s = _re.sub(r"\s+", "-", s)
    s = _re.sub(r"-{2,}", "-", s).strip("-")
    if len(s) > 50:
        parts = s.split("-")
        out = []
        for p in parts:
            if not p:
                continue
            if len("-".join(out + [p])) > 50:
                break
            out.append(p)
        s = "-".join(out) or s[:50]
    return s or f"post-{int(_dt.now().timestamp())}"

# ------------------------------
# ポリシー（統合）管理
# ------------------------------
CACHE_PATH = Path("./policies_cache.json")
DEFAULT_PRESET_NAME = "default"

DEFAULT_POLICY_TXT = """[リード文]
# リード文の作成指示:
・読者の悩みや不安を共感的に表現すること（例：「〜でお困りではありませんか」）
・この記事を読むことで得られる具体的なメリットを2つ以上提示すること
・「実は」「なんと」などの興味を引く表現を使うこと
・最後に行動を促す一文を入れること（例：「ぜひ最後までお読みください」）

[本文指示]
# 本文の作成指示:
・プロンプト③で出力された <h2> と <h3> 構成を維持し、それぞれの直下に <p> タグで本文を記述
・各 <h2> の冒頭に「ここでは、〜について解説します」形式の導入段落を3行程度 <p> タグで挿入する
・各 <h3> の直下には4～5文程度（400文字程度）の詳細な解説を記述
・<h4>、<script>、<style> などは禁止
・一文は55文字以内に収めること
・一文ごとに独立した<p>タグで記述すること（<br>タグは絶対に使用禁止）
・必要に応じて<ul>、<ol>、<li>、<table>、<tr>、<th>、<td>タグを使用して分かりやすく情報を整理すること
・各H2セクションには必ず1つ以上の表（table）または箇条書き（ul/ol）を含めること
・手続きの比較、メリット・デメリット、専門家比較、費用比較などは必ず表形式で整理すること
・メリット・デメリットの比較や専門家比較は必ず以下の形式で表を作成すること：
　<table><tr><th>項目</th><th>選択肢1</th><th>選択肢2</th></tr><tr><th>メリット</th><td>内容</td><td>内容</td></tr></table>
・表のHTMLタグ（table, tr, th, td）を正確に使用すること
・表形式が適している情報は必ず表で整理すること
・メリット・デメリットの比較は必ず表形式で作成すること
・【メリット】【デメリット】のような明確な区分を使用すること
・PREP法もしくはSDS法で書くこと
・横文字を使用しないこと
・冗長表現を使用しないこと
・「です」「ましょう」「ます」「ください」など、様々な語尾のバリエーションを使用してください
・具体例や注意点、実際の手続き方法を豊富に含め、実践的で有益な情報を提供すること
・専門的でありながら分かりやすい解説を心がけること
・情報量を増やすため、各セクションで詳細な説明と複数の具体例を含めること

[まとめ文]
# まとめ文の作成指示:
・必ず最初に<h2>{keyword}に関するまとめ</h2>を出力すること
・一文ごとに独立した<p>タグで記述すること（<br>タグは絶対に使用禁止）
・記事の要点を箇条書きで2-3個簡潔にリストも用いて文中に挿入すること
・内容は400文字程度にすること
"""

SECTION_MARKERS = ("[リード文]", "[本文指示]", "[まとめ文]")

def extract_sections(policy_text: str) -> Tuple[str, str, str]:
    def _find(label: str) -> str:
        m = re.search(rf"\[{label}\](.*?)(?=\[[^\]]+\]|$)", policy_text, flags=re.DOTALL)
        return (m.group(1).strip() if m else "")
    if not any(x in policy_text for x in SECTION_MARKERS):
        return "", policy_text.strip(), ""
    return _find("リード文"), _find("本文指示"), _find("まとめ文")

# ------------------------------
# キャッシュ I/O（統合テキストをそのまま保存）
# ------------------------------
def load_policies_from_cache() -> Dict[str, Any] | None:
    try:
        if CACHE_PATH.exists():
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        st.warning(f"ポリシーキャッシュ読込エラー: {e}")
    return None

def save_policies_to_cache(store: Dict[str, str], active_name: str):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"policy_store": store, "active_policy": active_name}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        st.warning(f"ポリシーキャッシュ保存エラー: {e}")

# ------------------------------
# サイト選択 & 疎通
# ------------------------------
st.sidebar.header("接続先（WP）")
site_key = st.sidebar.selectbox("投稿先サイト", sorted(WP_CONFIGS.keys()))
cfg = WP_CONFIGS[site_key]
BASE = ensure_trailing_slash(cfg["url"])
AUTH = HTTPBasicAuth(cfg["user"], cfg["password"])

if st.sidebar.button("🔐 認証 /users/me"):
    r = wp_get(BASE, "wp/v2/users/me", AUTH, HEADERS)
    st.sidebar.code(f"GET users/me → {r.status_code if r else 'N/A'}")
    st.sidebar.caption((r.text[:300] if r is not None else "No response"))

# ここに追加：モデル選択UI
st.sidebar.header("🤖 AIモデル選択")

model_choice = st.sidebar.radio(
    "使用するGeminiモデル",
    options=["Pro", "Flash"],
    index=0,
    help="Pro: 高品質（26円/記事） | Flash: 高速・低コスト（1.6円/記事）"
)

# モデル名をセッションに保存
if model_choice == "Pro":
    st.session_state["selected_model"] = "gemini-1.5-pro"
    st.sidebar.success("💎 Pro選択中\n約26円/記事（高品質）")
else:
    st.session_state["selected_model"] = "gemini-1.5-flash"  
    st.sidebar.info("⚡ Flash選択中\n約1.6円/記事（94%削減）")

st.sidebar.markdown("---")  # 区切り線

# ------------------------------
# セッション初期化（統合版）
# ------------------------------

if "policy_store" not in st.session_state or not isinstance(st.session_state.policy_store, dict):
    st.session_state.policy_store = {DEFAULT_PRESET_NAME: DEFAULT_POLICY_TXT}
if "active_policy" not in st.session_state:
    st.session_state.active_policy = DEFAULT_PRESET_NAME

cached = load_policies_from_cache()
if cached:
    cache_store = cached.get("policy_store")
    if isinstance(cache_store, dict) and cache_store:
        st.session_state.policy_store = cache_store
    ap = cached.get("active_policy")
    if ap in st.session_state.policy_store:
        st.session_state.active_policy = ap

if DEFAULT_PRESET_NAME not in st.session_state.policy_store:
    st.session_state.policy_store[DEFAULT_PRESET_NAME] = DEFAULT_POLICY_TXT
    if st.session_state.active_policy not in st.session_state.policy_store:
        st.session_state.active_policy = DEFAULT_PRESET_NAME

cur_txt = st.session_state.policy_store[st.session_state.active_policy]
st.session_state.setdefault("policy_text", cur_txt)
st.session_state.setdefault("banned_text", "")
st.session_state.setdefault("co_terms_text", "")  # 共起語入力

# ==============================
# 3カラム：入力 / 生成&プレビュー / 投稿
# ==============================
colL, colM, colR = st.columns([1.3, 1.6, 1.1])

# ------ 左：入力 / ポリシー管理(.txt) ------
with colL:
    st.header("1) 入力 & ポリシー管理（.txt）")

    keyword = st.text_input("必須キーワード", placeholder="例：先払い買取 口コミ")
    extra_points = st.text_area("特に加えてほしい内容（任意）", height=90)

    st.markdown("### 🔗 共起語（任意）")
    st.caption("改行またはカンマ区切り。本文に“自然に”散りばめます（例：審査, 即日, 最短, 手数料）。")
    co_terms_text = st.text_area("共起語リスト", value=st.session_state.get("co_terms_text", ""), height=120)
    st.session_state["co_terms_text"] = co_terms_text
    co_terms: List[str] = []
    if co_terms_text.strip():
        # カンマと改行の両対応→重複/空白除去
        raw_list = re.split(r"[,\n\r]+", co_terms_text)
        co_terms = sorted({w.strip() for w in raw_list if w.strip()})

    st.markdown("### 🚫 禁止事項（任意_1行=1項目）")
    banned_text = st.text_area("禁止ワード・禁止表現", value=st.session_state.get("banned_text", ""), height=120)
    st.session_state["banned_text"] = banned_text
    merged_banned = [l.strip() for l in banned_text.splitlines() if l.strip()]

    st.divider()
    st.subheader("④ 文章ポリシー（統合 .txt）")

    pol_files = st.file_uploader("policy*.txt（複数可）を読み込む", type=["txt"], accept_multiple_files=True)
    if pol_files:
        for f in pol_files:
            try:
                raw = f.read().decode("utf-8", errors="ignore").strip()
                name = f.name.rsplit(".", 1)[0]
                st.session_state.policy_store[name] = raw
                st.session_state.active_policy = name
                st.session_state.policy_text = raw
            except Exception as e:
                st.warning(f"{f.name}: 読み込み失敗 ({e})")
        save_policies_to_cache(st.session_state.policy_store, st.session_state.active_policy)

    names = sorted(st.session_state.policy_store.keys())
    sel_index = names.index(st.session_state.active_policy) if st.session_state.active_policy in names else 0
    sel_name = st.selectbox("適用するポリシー", names, index=sel_index)
    if sel_name != st.session_state.active_policy:
        st.session_state.active_policy = sel_name
        st.session_state.policy_text = st.session_state.policy_store[sel_name]
        save_policies_to_cache(st.session_state.policy_store, st.session_state.active_policy)

    st.markdown("### ✏️ 本文ルール")
    st.session_state.policy_text = st.text_area(
        "本文（統合形式）",
        value=st.session_state.get("policy_text", ""),
        height=420
    )

    cA, cB, cC, cD = st.columns([1, 1, 1, 1])
    with cA:
        if st.button("この内容で上書き保存"):
            st.session_state.policy_store[st.session_state.active_policy] = st.session_state.policy_text
            save_policies_to_cache(st.session_state.policy_store, st.session_state.active_policy)
            st.success(f"『{st.session_state.active_policy}』を更新しました。")
    with cB:
        st.download_button(
            "この内容をPCへ保存（.txt）",
            data=st.session_state.get("policy_text", ""),
            file_name=f"{st.session_state.active_policy}.txt",
            mime="text/plain",
            use_container_width=True
        )
    with cC:
        can_delete = (
            st.session_state.active_policy != DEFAULT_PRESET_NAME and
            len(st.session_state.policy_store) > 1 and
            st.session_state.active_policy in st.session_state.policy_store
        )
        delete_clicked = st.button("このプリセットを削除", disabled=not can_delete)
        if delete_clicked:
            del st.session_state.policy_store[st.session_state.active_policy]
            fallback = DEFAULT_PRESET_NAME if DEFAULT_PRESET_NAME in st.session_state.policy_store else None
            if not fallback:
                st.session_state.policy_store[DEFAULT_PRESET_NAME] = DEFAULT_POLICY_TXT
                fallback = DEFAULT_PRESET_NAME
            st.session_state.active_policy = fallback
            st.session_state.policy_text = st.session_state.policy_store[fallback]
            save_policies_to_cache(st.session_state.policy_store, st.session_state.active_policy)
            st.warning("プリセットを削除しました。")
    with cD:
        if st.button("🔁 プリセットを初期状態に戻す"):
            st.session_state.policy_store = {DEFAULT_PRESET_NAME: DEFAULT_POLICY_TXT}
            st.session_state.active_policy = DEFAULT_PRESET_NAME
            st.session_state.policy_text = DEFAULT_POLICY_TXT
            save_policies_to_cache(st.session_state.policy_store, st.session_state.active_policy)
            st.success("初期状態にリセットしました。")

# ------ 中：生成 & プレビュー ------
# ------ 中：生成 & プレビュー ------
with colM:
    st.header("2) 生成 & プレビュー")

    # H2最小/最大
    max_h2 = st.number_input("H2の最大数", min_value=3, max_value=12, value=MAX_H2, step=1)
    min_h2 = st.number_input("H2の最小数", min_value=1, max_value=12, value=3, step=1)
    if min_h2 > max_h2:
        st.warning("⚠️ H2の最小数が最大数を上回っています。最小≦最大 になるよう調整してください。")

    # 本文文字数
    min_chars = st.number_input("本文の最小文字数",  min_value=500,  max_value=20000, value=2000, step=100)
    max_chars = st.number_input("本文の最大文字数",  min_value=800,  max_value=30000, value=5000, step=100)
    strict_chars = st.checkbox("厳密制御（不足/超過を自動調整）", value=False)
    max_adjust_tries = st.number_input("自動調整の最大回数", 0, 3, 0, 1)


    # ①〜③ 生成
    if st.button("①〜③（読者像/ニーズ/構成）を生成"):
        if not keyword.strip():
            st.error("キーワードは必須です。")
            st.stop()
        outline_raw = call_gemini(
            prompt_outline_123(keyword, extra_points, merged_banned, co_terms, min_h2, max_h2),
            model=st.session_state.get("selected_model", "gemini-1.5-pro")
        )

        readers = re.search(r'①[^\n]*\n(.+?)\n\n②', outline_raw, flags=re.DOTALL)
        needs = re.search(r'②[^\n]*\n(.+?)\n\n③', outline_raw, flags=re.DOTALL)
        struct = re.search(r'③[^\n]*\n(.+)$', outline_raw, flags=re.DOTALL)

        st.session_state["readers"] = (readers.group(1).strip() if readers else "")
        st.session_state["needs"] = (needs.group(1).strip() if needs else "")
        structure_html = (struct.group(1).strip() if struct else "").replace("\r", "")
        structure_html = simplify_html(structure_html)

        if count_h2(structure_html) > max_h2:
            structure_html = trim_h2_max(structure_html, max_h2)

        current_h2 = count_h2(structure_html)
        if current_h2 < min_h2:
            need = min_h2 - current_h2
            add = call_gemini(prompt_fill_h2(keyword, structure_html, need), 
                  model=st.session_state.get("selected_model", "gemini-1.5-pro")).strip()
            add = simplify_html(add)
            if count_h2(add) > 0:
                structure_html = (structure_html.rstrip() + "\n\n" + add.strip())

        if count_h2(structure_html) > max_h2:
            structure_html = trim_h2_max(structure_html, max_h2)
      # --- ここから追加：最後のH2を必ず「まとめ」に固定する ---
        # ユーザーの min/max は「総H2数（= まとめ含む）」として扱う。
        # ③では本文用H2のみ(total_h2-1)を確定させ、最後の1枠をまとめに予約する。
        target_total_h2 = max_h2  # 「6個で指定」等、上限＝総数として解釈
        structure_html = enforce_summary_last(structure_html, keyword, target_total_h2)
        # --- 追加ここまで ---

        st.session_state["structure_html"] = structure_html

    # 手直し
    readers_txt = st.text_area("① 読者像（編集可）", value=st.session_state.get("readers", ""), height=110)
    needs_txt = st.text_area("② ニーズ（編集可）", value=st.session_state.get("needs", ""), height=110)
    structure_html = st.text_area("③ 構成（HTML / 編集可）", value=st.session_state.get("structure_html", ""), height=180)

    # 記事を一括生成
    if st.button("🪄 記事を一括生成（リード→本文→まとめ）", type="primary", use_container_width=True):
        if not keyword.strip():
            st.error("キーワードは必須です。"); st.stop()
        if not structure_html.strip():
            st.error("③構成（HTML）が必要です。①〜③を生成し、必要なら編集してください。"); st.stop()

        full = call_gemini(
            prompt_full_article_unified(
                keyword=keyword,
                unified_policy_text=st.session_state.policy_text,
                structure_html=structure_html,
                readers_txt=readers_txt,
                needs_txt=needs_txt,
                banned=merged_banned,
                co_terms=co_terms,
                min_chars=min_chars,
                max_chars=max_chars
            ),
            model=st.session_state.get("selected_model", "gemini-1.5-pro")
        )
        full = simplify_html(full)
        st.session_state["assembled_html"] = full
        st.session_state["edited_html"] = full
        st.session_state["use_edited"] = True

        html_cur = st.session_state.get("edited_html", "")
        if html_cur and not _has_summary(html_cur):
            st.info("自動ガード: まとめが見つからなかったため、ローカルで補完しました。")
            st.session_state["edited_html"] = _append_fallback_summary(html_cur)

        # 文字数厳密制御
        if strict_chars:
            tries = 0
            html_cur = st.session_state["edited_html"]
            while tries < max_adjust_tries:
                cur_len = visible_length(html_cur)
                if cur_len < min_chars:
                    need = min(min_chars - cur_len, max_chars - cur_len)
                    if need <= 0: break
                    try:
                        add = call_gemini(prompt_append_chars(keyword, co_terms, content, needed_chars), 
                        model=st.session_state.get("selected_model", "gemini-1.5-pro")).strip()
                        add = simplify_html(add)
                        if not add or visible_length(add) < 100:
                            break
                        html_cur = (html_cur.rstrip() + "\n\n" + add)
                    except Exception:
                        break
                elif cur_len > max_chars:
                    html_cur = trim_to_max_chars(html_cur, max_chars)
                    break
                else:
                    break
                tries += 1
            st.session_state["edited_html"] = html_cur

            # --- まとめの長さをローカルで強制キャップ（追加料金ゼロ） ---
            html_cur = st.session_state.get("edited_html", "")
            if html_cur:
                # まとめは約300字目安 → 上限320字でキャップ
                html_cur = cap_summary(html_cur, limit_chars=320)
                # 全体が上限を超える場合は最後に安全カット
                if visible_length(html_cur) > max_chars:
                    html_cur = trim_to_max_chars(html_cur, max_chars)
                st.session_state["edited_html"] = html_cur
                st.session_state["assembled_html"] = html_cur  # プレビュー側も同期
    

    # プレビュー & 編集
    assembled = st.session_state.get("assembled_html", "")
    if assembled:
        st.markdown("#### 👀 プレビュー（一括生成結果）")
        st.write(assembled, unsafe_allow_html=True)
        issues = validate_article(assembled)

        # 共起語の出現チェック（大小無視・単純包含）
        if co_terms:
            plain = re.sub(r'<.*?>', '', assembled).lower()
            missing = [w for w in co_terms if w.lower() not in plain]
            if missing:
                issues.append(f"共起語が本文に見当たりません：{', '.join(missing)}")

        if issues:
            st.warning("検査結果:\n- " + "\n- ".join(issues))

    with st.expander("✏️ プレビューを編集（この内容を下書きに送付）", expanded=False):
        st.caption("※ ここでの修正が最終本文になります。HTMLで編集可。")
        st.session_state["edited_html"] = st.text_area(
            "編集用HTML",
            value=st.session_state.get("edited_html", assembled),
            height=420
        )
        st.session_state["use_edited"] = st.checkbox("編集したHTMLを採用する", value=True)

# ------ 右：タイトル/説明 → 投稿 ------
# ------ 右：タイトル/説明 → 投稿 ------
with colR:
    st.header("3) タイトル/説明 → 投稿")

    content_dir = (st.session_state.get("readers", "") + "\n" +
                   st.session_state.get("needs", "") + "\n" +
                   (st.session_state.get("policy_text", "")))
    content_source = st.session_state.get("edited_html") or st.session_state.get("assembled_html", "")

    # 統合生成ボタン
    if st.button("📝 SEOタイトル・説明文を自動生成", use_container_width=True):
        if not content_source.strip():
            st.warning("先に本文（編集後）を用意してください。")
        else:
            with st.spinner("タイトルと説明文を生成中..."):
                title, desc = generate_title_and_description_unified(keyword, content_dir)
                st.session_state["title"] = title
                st.session_state["excerpt"] = desc
                st.success(f"生成完了！ タイトル: {len(title)}文字 / 説明文: {len(desc)}文字")

    # 個別生成ボタン（バックアップ）
    with st.expander("🔧 個別生成（統合版で上手くいかない場合）", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            if st.button("タイトルのみ生成"):
                if not content_source.strip():
                    st.warning("先に本文を用意してください。")
                else:
                    st.session_state["title"] = generate_seo_title(keyword, content_dir)
                    st.success("タイトルを生成しました。")
        with col2:
            if st.button("説明文のみ生成"):
                if not content_source.strip():
                    st.warning("先に本文を用意してください。")
                else:
                    t = st.session_state.get("title", "") or f"{keyword}について"
                    st.session_state["excerpt"] = generate_seo_description(keyword, content_dir, t)
                    st.success("説明文を生成しました。")

    title = st.text_input("タイトル", value=st.session_state.get("title", ""))
    slug = st.text_input("スラッグ（空ならキーワード/タイトルから自動）", value="")
    excerpt = st.text_area("ディスクリプション（抜粋）", value=st.session_state.get("excerpt", ""), height=80)

    # ▼ カテゴリーUI（Secrets→wp_categories→REST）
    def fetch_categories(base_url: str, auth: HTTPBasicAuth) -> List[Tuple[str, int]]:
        try:
            r = wp_get(base_url, "wp/v2/categories?per_page=100&_fields=id,name", auth, HEADERS)
            if r is not None and r.status_code == 200:
                data = r.json()
                pairs = [(c.get("name", "(no name)"), int(c.get("id"))) for c in data if c.get("id") is not None]
                return sorted(pairs, key=lambda x: x[0])
        except Exception:
            pass
        return []

    cfg_cats_map: Dict[str, int] = dict(cfg.get("categories", {}))
    cats: List[Tuple[str, int]] = []
    if cfg_cats_map:
        cats = sorted([(name, int(cid)) for name, cid in cfg_cats_map.items()], key=lambda x: x[0])
    else:
        sc_map: Dict[str, int] = st.secrets.get("wp_categories", {}).get(site_key, {})
        if sc_map:
            cats = sorted([(name, int(cid)) for name, cid in sc_map.items()], key=lambda x: x[0])
        else:
            cats = fetch_categories(BASE, AUTH)

    cat_labels = [name for (name, _cid) in cats]
    sel_labels: List[str] = st.multiselect("カテゴリー（複数可）", cat_labels, default=[])
    selected_cat_ids: List[int] = [cid for (name, cid) in cats if name in sel_labels]
    if not cats:
        st.info("このサイトで選べるカテゴリーが見つかりませんでした。Secretsの `wp_configs.<site_key>.categories` を確認してください。")

    # 公開状態（日本語ラベル → API値）
    status_options = {"下書き": "draft", "予約投稿": "future", "公開": "publish"}
    status_label = st.selectbox("公開状態", list(status_options.keys()), index=0)
    status = status_options[status_label]
    sched_date = st.date_input("予約日（future用）")
    sched_time = st.time_input("予約時刻（future用）", value=dt_time(9, 0))

    # 投稿
    if st.button("📝 WPに下書き/投稿する", type="primary", use_container_width=True):
        if not keyword.strip():
            st.error("キーワードは必須です。"); st.stop()
        if not title.strip():
            st.error("タイトルは必須です。"); st.stop()

        content_html = (st.session_state.get("edited_html") if st.session_state.get("use_edited")
                        else st.session_state.get("assembled_html", "")).strip()
        if not content_html:
            st.error("本文が未生成です。『①〜③生成→記事を一括生成』の順で作成してください。"); st.stop()

        content_html = simplify_html(content_html)

        date_gmt = None
        if status == "future":
            from datetime import datetime as _dt
            dt_local = _dt.combine(sched_date, sched_time)
            date_gmt = dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        # スラッグ決定
        typed_slug = slug.strip() if slug else ""
        final_slug = typed_slug or generate_permalink(title or keyword)

        payload = {
            "title": title.strip(),
            "content": content_html,
            "status": status,
            "excerpt": excerpt.strip()
        }
        if date_gmt:
            payload["date_gmt"] = date_gmt
        if final_slug:
            payload["slug"] = final_slug
        if selected_cat_ids:
            payload["categories"] = selected_cat_ids

        r = wp_post(BASE, "wp/v2/posts", AUTH, HEADERS, json_payload=payload)
        if r is None or r.status_code not in (200, 201):
            st.error(f"投稿失敗: {r.status_code if r else 'N/A'}")
            if r is not None:
                st.code(r.text[:1000])
            st.stop()

        data = r.json()
        st.success(f"投稿成功！ID={data.get('id')} / status={data.get('status')}")
        st.write("URL:", data.get("link", ""))
        st.json({k: data.get(k) for k in ["id", "slug", "status", "date", "link"]})


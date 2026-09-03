"""キュレーター入力を既存ブランドDBへ昇格させる

経路:
  curator_picks_sandbox (4項目の生入力)
    → 既存 brands_sandbox との重複判定
    → 不足項目を Gemini が公式サイトから補完
    → brands_sandbox (status='draft') + brand_urls_sandbox へ投入
    → 既存の draft レビュー画面 (ui/app.py) で active へ昇格

設計の考え方:
  堤さんが書いた「おすすめの理由」は AI に書けない一次情報なので、
  curator_rationale にそのまま残す。要約も書き換えもしない。
  AI が埋めるのは、公式サイトを読めば分かる範囲（哲学・カテゴリ・価格帯）だけに限る。

使用例:
  python scripts/promote_curator_picks.py --dry-run     # 何が昇格するか確認
  python scripts/promote_curator_picks.py               # 実行
  python scripts/promote_curator_picks.py --curator tsutsumi --limit 10
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client

# Windows の既定コンソールは cp932 のため、記号を含む出力で落ちることがある
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env.local")

PICKS_TABLE = "curator_picks_sandbox"
BRANDS_TABLE = "brands_sandbox"
URLS_TABLE = "brand_urls_sandbox"
LOCAL_STORE = ROOT / "seed" / "curator_picks.jsonl"

CURATOR_NAMES = {"tsutsumi": "堤"}
GEMINI_MODEL = "gemini-2.5-flash"


# --------------------------------------------------------------- 補助
def norm_url(u: str) -> str:
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def make_brand_id(name: str, url: str, taken: set[str]) -> str:
    """既存の命名規則に合わせる（例: jp__komiya_shoten）"""
    host = norm_url(url).split("/")[0]
    tld = host.rsplit(".", 1)[-1] if "." in host else "jp"
    country = {"jp": "jp", "com": "jp", "net": "jp", "co": "jp"}.get(tld, tld)
    slug = re.sub(r"[^a-z0-9]+", "_", host.split(".")[0]).strip("_") or "brand"
    base = f"{country}__{slug}"
    cand, n = base, 2
    while cand in taken:
        cand, n = f"{base}_{n}", n + 1
    return cand


def load_picks(sb: Client | None, curator: str | None) -> list[dict]:
    if sb:
        q = sb.table(PICKS_TABLE).select("*").eq("status", "submitted")
        if curator:
            q = q.eq("curator", curator)
        return q.order("created_at").execute().data or []
    if not LOCAL_STORE.exists():
        return []
    rows = [json.loads(l) for l in LOCAL_STORE.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [r for r in rows if (not curator or r.get("curator") == curator)
            and r.get("status", "submitted") == "submitted"]


def existing_index(sb: Client) -> tuple[dict[str, str], dict[str, str], set[str]]:
    brands = sb.table(BRANDS_TABLE).select("id,name_ja").execute().data or []
    urls = sb.table(URLS_TABLE).select("brand_id,official_url").execute().data or []
    by_name = {b["name_ja"]: b["id"] for b in brands if b.get("name_ja")}
    by_url = {norm_url(u["official_url"]): u["brand_id"] for u in urls if u.get("official_url")}
    return by_name, by_url, {b["id"] for b in brands}


# --------------------------------------------------------------- AI 補完
ENRICH_PROMPT = """次のブランドについて、公式サイトの内容をもとに項目を埋めてください。
分からない項目は null にしてください。推測で断定的な事実を書かないでください。

ブランド名: {name}
公式サイト: {url}
このブランドを推薦した人のコメント: {reason}

以下のJSONのみを返してください。
{{
  "name_en": "英字表記かnull",
  "origin_country": "jp などの2文字",
  "origin_region": "都道府県や都市。分からなければnull",
  "founded_year": 数値かnull,
  "philosophy_one_liner": "40字程度でブランドの立ち位置",
  "philosophy_long": "200-400字。歴史・製法・思想",
  "philosophy_keywords": ["3-6語"],
  "aesthetic_tags": ["minimal/warm/traditional/modern/natural/artisan から該当するもの"],
  "main_categories": ["food/interior/kitchen/tableware/apparel/textile/wood_craft/metal_craft/ceramics/glassware/stationery/bag/leather_goods/bath_body/fragrance/jewelry/watch/outdoor/knife/paper_craft/lacquerware/lighting/tea/sake/toy から該当するもの"],
  "sub_categories": ["具体的な品目を英語スネークケースで"],
  "price_tiers": [{{"role":"entry","label":"品目名","min_jpy":0,"max_jpy":0}}],
  "gift_format": ["boxed/wrappable/fragile/perishable から該当するもの"],
  "recipient_fit_freetext": "どんな相手に向くか。100字程度"
}}"""


def enrich(pick: dict) -> dict | None:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        return None
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        prompt = ENRICH_PROMPT.format(name=pick["brand_name"], url=pick["official_url"],
                                      reason=pick.get("brand_reason") or "")
        res = client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                response_mime_type="text/plain"))
        text = re.sub(r"^```(?:json)?|```$", "", (res.text or "").strip(), flags=re.M).strip()
        return json.loads(text)
    except Exception as e:
        print(f"    [warn] 補完に失敗: {e}", file=sys.stderr)
        return None


PLACEHOLDER = {
    "origin_country": "jp",
    "philosophy_one_liner": "（未補完）",
    "philosophy_long": "（未補完）",
    "philosophy_keywords": [],
    "aesthetic_tags": [],
    "main_categories": [],
    "sub_categories": [],
    "price_tiers": [],
    "gift_format": [],
}


def to_brand_row(pick: dict, brand_id: str, enriched: dict | None) -> dict:
    e = enriched or {}
    curator_label = CURATOR_NAMES.get(pick.get("curator", ""), pick.get("curator", "curator"))
    reason = (pick.get("brand_reason") or "").strip()
    products = pick.get("products") or []
    if not products and pick.get("product_name"):        # 旧形式のデータ
        products = [{"name": pick["product_name"],
                     "reason": pick.get("product_reason") or ""}]
    for pr in products:
        reason += chr(10) + "推薦商品: " + str(pr.get("name") or "")
        if pr.get("reason"):
            reason += " " + str(pr["reason"])
        if pr.get("url"):
            reason += chr(10) + "  " + str(pr["url"])

    row = {
        "id": brand_id,
        "name_ja": pick["brand_name"],
        "status": "draft",
        # 堤さんの言葉はそのまま残す。要約も書き換えもしない
        "curator_rationale": reason,
        "curator_added_by": curator_label,
        "curator_source": f"curator_{pick.get('layer', 'known')}",
        "curator_added_date": str(date.today()),
    }
    for k, v in PLACEHOLDER.items():
        row[k] = e.get(k) if e.get(k) not in (None, "") else v
    for k in ("name_en", "origin_region", "founded_year", "recipient_fit_freetext"):
        if e.get(k):
            row[k] = e[k]
    return row


# --------------------------------------------------------------- 本体
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--curator", help="対象キュレーター（省略時は全員）")
    ap.add_argument("--limit", type=int, help="処理件数の上限")
    ap.add_argument("--dry-run", action="store_true", help="投入せず内容だけ表示")
    ap.add_argument("--no-enrich", action="store_true", help="AI補完を行わない")
    a = ap.parse_args()

    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    sb = create_client(url, key) if url and key else None
    if not sb:
        print("[INFO] Supabase 未設定。ローカルの控えを読んで内容確認のみ行います")

    picks = load_picks(sb, a.curator)
    if a.limit:
        picks = picks[:a.limit]
    if not picks:
        print("[INFO] 昇格対象がありません")
        return 0

    by_name, by_url, taken = existing_index(sb) if sb else ({}, {}, set())
    n_new = n_dup = 0

    for p in picks:
        nurl = norm_url(p["official_url"])
        hit = by_url.get(nurl) or by_name.get(p["brand_name"])
        if hit:
            n_dup += 1
            print(f"[重複] {p['brand_name']} → 既存 {hit}")
            if sb and not a.dry_run:
                sb.table(PICKS_TABLE).update(
                    {"status": "reviewed", "promoted_brand_id": hit,
                     "review_notes": "既存ブランドと重複。理由は既存側へ手動で追記する"}
                ).eq("id", p["id"]).execute()
            continue

        brand_id = make_brand_id(p["brand_name"], p["official_url"], taken)
        enriched = None if a.no_enrich else enrich(p)
        row = to_brand_row(p, brand_id, enriched)
        n_new += 1
        mark = "補完あり" if enriched else "補完なし"
        print(f"[新規] {p['brand_name']} → {brand_id}（{mark}）")
        if a.dry_run:
            print("       " + json.dumps(
                {k: row[k] for k in ("philosophy_one_liner", "main_categories", "curator_rationale")},
                ensure_ascii=False)[:180])
            continue
        if not sb:
            continue
        sb.table(BRANDS_TABLE).insert(row).execute()
        sb.table(URLS_TABLE).insert(
            {"brand_id": brand_id, "official_url": p["official_url"]}).execute()
        sb.table(PICKS_TABLE).update(
            {"status": "promoted", "promoted_brand_id": brand_id}).eq("id", p["id"]).execute()
        taken.add(brand_id)

    print(f"\n[DONE] 新規 {n_new} 件 / 既存と重複 {n_dup} 件"
          + ("（dry-run のため未投入）" if a.dry_run else ""))
    print("投入したものは status='draft' です。ui/app.py の draft レビューで active へ昇格してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""キュレーター用 ブランド登録アプリ

会議 (2026-09-01) の決定に沿った、入力だけに絞った画面。
- 入力は4項目のみ（ブランド名 / 公式URL / おすすめ理由 / おすすめ商品とその理由）
- タグ・カテゴリ・価格帯などは入力しない（後段で AI が補う）
- 2つのステップを切り替えて集める
    ステップ1 知っているもの   : 元から知っていて、おすすめできるブランド
    ステップ2 見つけたもの     : 調べていて出会い、ピンときたブランド

起動:
  streamlit run streamlit_app.py
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

try:
    from supabase import Client, create_client
except Exception:                                    # ローカル閲覧のみの環境でも起動できるようにする
    Client = None
    create_client = None

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env.local")          # 手元起動時の接続情報。Cloud では st.secrets を使う
LOCAL_STORE = ROOT / "curator_picks.jsonl"
TABLE = "curator_picks_sandbox"
BRANDS_TABLE = "brands_sandbox"
URLS_TABLE = "brand_urls_sandbox"
CURATOR_DEFAULT = "tsutsumi"

LAYERS = {
    "known": "ステップ1  知っているもの",
    "explored": "ステップ2  見つけたもの",
}


# ----------------------------------------------------------------- 接続
def _secret(key: str, default: str | None = None) -> str | None:
    try:
        v = st.secrets.get(key)
        if v is not None:
            return str(v)
    except Exception:
        pass
    return os.environ.get(key, default)


@st.cache_resource(show_spinner=False)
def _client():
    """Supabase に繋がればそれを使い、繋がらなければローカル保存に切り替える"""
    url, key = _secret("SUPABASE_URL"), _secret("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key and create_client):
        return None
    try:
        return create_client(url, key)
    except Exception:
        return None


def _password_ok() -> bool:
    expected = _secret("CURATOR_PASSWORD") or _secret("ADMIN_PASSWORD")
    if not expected:
        return True
    if st.session_state.get("_authed"):
        return True
    with st.form("auth"):
        st.markdown("#### 合言葉を入力してください")
        pw = st.text_input("合言葉", type="password", label_visibility="collapsed")
        if st.form_submit_button("入る", use_container_width=True):
            if pw == expected:
                st.session_state["_authed"] = True
                st.rerun()
            else:
                st.error("合言葉が違います")
    return False


# ----------------------------------------------------------------- 保存と読み出し
def _local_rows() -> list[dict]:
    if not LOCAL_STORE.exists():
        return []
    rows = []
    for line in LOCAL_STORE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def load_rows(curator: str) -> list[dict]:
    sb = _client()
    if sb:
        try:
            res = (sb.table(TABLE).select("*")
                   .eq("curator", curator).order("created_at", desc=True).execute())
            return res.data or []
        except Exception as e:
            st.warning(f"サーバーから読めなかったので、この端末の記録を表示しています（{e}）")
    return [r for r in _local_rows() if r.get("curator") == curator][::-1]


def save_row(row: dict) -> tuple[bool, str]:
    sb = _client()
    if sb:
        try:
            sb.table(TABLE).insert(row).execute()
            return True, "サーバーに保存しました"
        except Exception as e:
            msg = str(e)
            if "duplicate key" in msg or "23505" in msg:
                return False, "このURLはすでに登録されています"
            st.warning(f"サーバーに保存できなかったので、この端末に控えました（{e}）")
    LOCAL_STORE.parent.mkdir(parents=True, exist_ok=True)
    with LOCAL_STORE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({**row, "created_at": datetime.now().isoformat()},
                           ensure_ascii=False) + "\n")
    return True, "この端末に保存しました"


def delete_row(row_id, curator: str) -> None:
    sb = _client()
    if sb and isinstance(row_id, int):
        try:
            sb.table(TABLE).delete().eq("id", row_id).execute()
            return
        except Exception as e:
            st.warning(f"サーバーから消せませんでした（{e}）")
    rows = [r for r in _local_rows()
            if not (r.get("official_url") == row_id and r.get("curator") == curator)]
    LOCAL_STORE.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


# ----------------------------------------------------------------- 既存ブランドとの照合
def _norm_url(u: str) -> str:
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


@st.cache_data(ttl=600, show_spinner=False)
def existing_brands() -> list[dict]:
    """既存 brands_sandbox の名前とURL。重複登録を入力時点で止めるために使う"""
    sb = _client()
    if not sb:
        return []
    try:
        brands = sb.table(BRANDS_TABLE).select("id,name_ja").execute().data or []
        urls = sb.table(URLS_TABLE).select("brand_id,official_url").execute().data or []
        by_id = {b["id"]: b.get("name_ja") for b in brands}
        out = [{"id": b["id"], "name": b.get("name_ja"), "url": None} for b in brands]
        for u in urls:
            out.append({"id": u["brand_id"], "name": by_id.get(u["brand_id"]),
                        "url": _norm_url(u.get("official_url"))})
        return out
    except Exception:
        return []


def find_existing(name: str, url: str) -> dict | None:
    """すでに登録済みのブランドなら、その情報を返す"""
    nurl, nname = _norm_url(url), (name or "").strip()
    for r in existing_brands():
        if nurl and r.get("url") and r["url"] == nurl:
            return {**r, "kind": "URL"}
    for r in existing_brands():
        if nname and r.get("name") and r["name"] == nname:
            return {**r, "kind": "ブランド名"}
    return None


def find_own_duplicate(url: str, curator: str) -> dict | None:
    """自分が過去に登録したものと重複していないか"""
    nurl = _norm_url(url)
    for r in load_rows(curator):
        if _norm_url(r.get("official_url", "")) == nurl:
            return r
    return None


# ----------------------------------------------------------------- 入力チェック
URL_RE = re.compile(r"^https?://[^\s]+$")


def check(name: str, url: str, brand_reason: str) -> list[str]:
    errs = []
    if not name.strip():
        errs.append("ブランド名を入れてください")
    if not url.strip():
        errs.append("公式サイトのURLを入れてください")
    elif not URL_RE.match(url.strip()):
        errs.append("URLは https:// から始まる形で入れてください")
    if not brand_reason.strip():
        errs.append("おすすめの理由を一言でも書いてください")
    return errs


# ----------------------------------------------------------------- 画面
def render_form(layer: str, curator: str) -> None:
    if layer == "known":
        st.info("**知っているブランドを登録します。** "
                "あなたが元から知っていて、贈り物としておすすめできるものを入れてください。")
    else:
        st.info("**調べていて見つけたブランドを登録します。** "
                "なぜピンときたのか、その理由がいちばん大事な情報になります。")

    with st.form(f"form_{layer}", clear_on_submit=True):
        c1, c2 = st.columns([1, 1.4])
        with c1:
            name = st.text_input("ブランド名 *", placeholder="小宮商店")
        with c2:
            url = st.text_input("公式サイトのURL *", placeholder="https://www.komiyakasa.jp/")

        brand_reason = st.text_area(
            "このブランドをおすすめする理由 *", height=110,
            placeholder=("ホームページに書いてあることの写しではなく、あなたの言葉で。"
                         "\n例）修理しながら永く使うことを前提に作っている。"
                         "職人が一本ずつ手で仕上げていて、直して使う文化そのものを掲げている。"),
            help="ここが最も価値のある情報です。AIには書けない部分なので、思ったことをそのまま書いてください。")

        st.markdown("###### おすすめの商品（あれば）")
        c3, c4 = st.columns([1, 2])
        with c3:
            product = st.text_input("商品名", placeholder="折りたたみ傘",
                                    label_visibility="collapsed")
        with c4:
            product_reason = st.text_input(
                "その商品をおすすめする理由", placeholder="この一本にブランドの考えがいちばん出ている",
                label_visibility="collapsed")

        st.caption("商品が思い当たらなければ空欄で構いません。ブランドだけの登録でも十分です。")
        submitted = st.form_submit_button("登録する", use_container_width=True, type="primary")

    if submitted:
        errs = check(name, url, brand_reason)
        if errs:
            for e in errs:
                st.error(e)
            return

        dup = find_own_duplicate(url, curator)
        if dup:
            st.error(f"このURLは「{dup.get('brand_name')}」として登録済みです")
            return

        hit = find_existing(name, url)
        if hit:
            st.warning(
                f"**「{hit.get('name')}」は既にブランドDBに入っています**（{hit['kind']}が一致）。"
                "　それでも登録すると、あなたの理由が既存のブランドに追記される形になります。"
                "重複ではなく理由を足したい場合は、そのまま登録して構いません。")
            st.session_state[f"force_{layer}"] = {
                "brand_name": name.strip(), "official_url": url.strip(),
                "brand_reason": brand_reason.strip(),
                "product_name": product.strip() or None,
                "product_reason": product_reason.strip() or None,
                "layer": layer, "curator": curator,
                "existing_brand_id": hit.get("id"),
            }
            return

        ok, msg = save_row({
            "brand_name": name.strip(),
            "official_url": url.strip(),
            "brand_reason": brand_reason.strip(),
            "product_name": product.strip() or None,
            "product_reason": product_reason.strip() or None,
            "layer": layer,
            "curator": curator,
        })
        if ok:
            st.success(f"{name.strip()} を登録しました（{msg}）")
        else:
            st.error(msg)

    pending = st.session_state.get(f"force_{layer}")
    if pending:
        c1, c2 = st.columns(2)
        if c1.button("それでも登録する", key=f"force_ok_{layer}", use_container_width=True):
            row = {k: v for k, v in pending.items() if k != "existing_brand_id"}
            row["review_notes"] = f"既存ブランド {pending.get('existing_brand_id')} と重複の可能性"
            ok, msg = save_row(row)
            st.session_state.pop(f"force_{layer}", None)
            st.success(f"登録しました（{msg}）") if ok else st.error(msg)
            st.rerun()
        if c2.button("やめる", key=f"force_no_{layer}", use_container_width=True):
            st.session_state.pop(f"force_{layer}", None)
            st.rerun()


def render_list(curator: str) -> None:
    rows = load_rows(curator)
    if not rows:
        st.caption("まだ登録がありません。")
        return

    n_known = sum(1 for r in rows if r.get("layer") == "known")
    n_exp = len(rows) - n_known
    c1, c2, c3 = st.columns(3)
    c1.metric("登録数", len(rows))
    c2.metric("知っているもの", n_known)
    c3.metric("見つけたもの", n_exp)

    st.markdown("---")
    for r in rows:
        head = f"**{r.get('brand_name')}**　" \
               f"<span style='color:#6b7480;font-size:0.85em'>{LAYERS.get(r.get('layer'), '')}</span>"
        st.markdown(head, unsafe_allow_html=True)
        st.markdown(f"[{r.get('official_url')}]({r.get('official_url')})")
        if r.get("brand_reason"):
            st.markdown(f"　{r['brand_reason']}")
        if r.get("product_name"):
            st.markdown(f"　**{r['product_name']}** — {r.get('product_reason') or ''}")
        key = r.get("id") if isinstance(r.get("id"), int) else r.get("official_url")
        if st.button("削除", key=f"del_{key}"):
            delete_row(key, curator)
            st.rerun()
        st.markdown("---")


def render_export(curator: str) -> None:
    rows = load_rows(curator)
    if not rows:
        st.caption("まだ登録がありません。")
        return
    df = pd.DataFrame(rows)
    cols = [c for c in ["brand_name", "official_url", "brand_reason",
                        "product_name", "product_reason", "layer", "created_at"]
            if c in df.columns]
    df = df[cols]
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button("CSVで書き出す", df.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"curator_picks_{curator}.csv", mime="text/csv")


def main() -> None:
    st.set_page_config(page_title="ブランド登録", layout="centered")
    if not _password_ok():
        return

    st.title("ブランド登録")
    curator = _secret("CURATOR_NAME") or CURATOR_DEFAULT

    if _client() is None:
        st.caption("サーバーに繋がっていないため、この端末に保存します。"
                   "あとでまとめて送れるので、そのまま入力を続けて構いません。")

    tab_known, tab_explored, tab_list, tab_export = st.tabs(
        ["知っているもの", "見つけたもの", "登録した一覧", "書き出し"])
    with tab_known:
        render_form("known", curator)
    with tab_explored:
        render_form("explored", curator)
    with tab_list:
        render_list(curator)
    with tab_export:
        render_export(curator)


if __name__ == "__main__":
    main()

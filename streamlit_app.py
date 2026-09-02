"""キュレーター用 ブランド登録アプリ

会議 (2026-09-01) の決定に沿った、入力だけに絞った画面。
- 入力は4項目のみ（ブランド名 / 公式URL / おすすめ理由 / おすすめ商品とその理由）
- タグ・カテゴリ・価格帯などは入力しない（後段で AI が補う）
- 2つのステップを切り替えて集める
    ステップ1 知っているもの   : 元から知っていて、おすすめできるブランド
    ステップ2 見つけたもの     : 調べていて出会い、ピンときたブランド

起動:
  streamlit run ui/curator.py
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


def _on_cloud() -> bool:
    """Streamlit Community Cloud で動いているか。Cloud のファイルは再起動で消える"""
    return bool(os.environ.get("HOSTNAME", "").startswith("streamlit")
                or os.environ.get("STREAMLIT_SHARING_MODE")
                or os.path.exists("/mount/src"))


@st.cache_resource(show_spinner=False)
def _build_client(url: str, key: str):
    """接続情報ごとに1つ作る。引数に含めるのは、設定を書き換えたときに
    古い接続先を掴んだままにしないため（Cloud では設定変更後もプロセスが残る）"""
    try:
        return create_client(url, key)
    except Exception:
        return None


def _client():
    """Supabase に繋がればそれを使い、繋がらなければローカル保存に切り替える"""
    url, key = _secret("SUPABASE_URL"), _secret("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key and create_client):
        return None
    return _build_client(url, key)


@st.cache_data(ttl=60, show_spinner=False)
def _connection_status() -> tuple[bool, str]:
    """接続できるかを実際に1回問い合わせて確かめる。失敗理由もそのまま返す"""
    url = _secret("SUPABASE_URL")
    if not url:
        return False, "SUPABASE_URL が設定されていません"
    if not _secret("SUPABASE_SERVICE_ROLE_KEY"):
        return False, "SUPABASE_SERVICE_ROLE_KEY が設定されていません"
    sb = _client()
    if sb is None:
        return False, "接続の準備ができませんでした"
    try:
        sb.table(TABLE).select("id").limit(1).execute()
        return True, ""
    except Exception as e:
        msg = str(e)
        host = url.replace("https://", "").replace("http://", "").rstrip("/")
        if "Name or service not known" in msg or "getaddrinfo" in msg:
            import re as _re
            m = _re.search(r"[A-Za-z0-9._-]+\.supabase\.co", msg)
            actual = m.group(0) if m else host
            note = "" if actual == host else f"（設定値は `{host}`。食い違っています）"
            return False, (f"接続先 `{actual}` が見つかりません。"
                           f"SUPABASE_URL の綴りを確認してください{note}")
        if "Invalid API key" in msg or "JWT" in msg or "401" in msg:
            return False, "キーが正しくありません。service_role キーか確認してください"
        if "does not exist" in msg or "PGRST205" in msg:
            return False, f"テーブル {TABLE} がありません。DDL を流してください"
        return False, f"接続先 `{host}` へ繋がりません: {msg[:120]}"


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


def _products_to_legacy(row: dict) -> dict:
    """旧カラムにも先頭商品を入れておく（過去データと並べて見るため）"""
    ps = row.get("products") or []
    row["product_name"] = ps[0]["name"] if ps else None
    row["product_reason"] = ps[0].get("reason") or None if ps else None
    return row


def save_row(row: dict) -> tuple[bool, str]:
    row = _products_to_legacy(dict(row))
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


def update_row(row_id, curator: str, patch: dict) -> tuple[bool, str]:
    patch = _products_to_legacy(dict(patch))
    sb = _client()
    if sb and isinstance(row_id, int):
        try:
            sb.table(TABLE).update(patch).eq("id", row_id).execute()
            return True, "更新しました"
        except Exception as e:
            return False, f"更新できませんでした（{e}）"
    rows = _local_rows()
    for r in rows:
        if r.get("official_url") == row_id and r.get("curator") == curator:
            r.update(patch)
    LOCAL_STORE.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + chr(10) for r in rows),
        encoding="utf-8")
    return True, "更新しました"


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
def _seed_products(prefix: str, initial: list[dict] | None = None) -> None:
    """商品欄の初期値を一度だけ置く。行は連番のidで持ち、途中を消しても
    他の行の入力がずれないようにする"""
    ids_key = f"{prefix}__ids"
    if ids_key in st.session_state:
        return
    initial = list(initial or []) or [{}]
    st.session_state[ids_key] = list(range(len(initial)))
    st.session_state[f"{prefix}__next"] = len(initial)
    for i, pr in enumerate(initial):
        st.session_state[f"{prefix}__pname_{i}"] = pr.get("name", "")
        st.session_state[f"{prefix}__preason_{i}"] = pr.get("reason", "")


def _product_inputs(prefix: str) -> None:
    """商品は1組ずつの入力欄で受ける。表（data_editor）を使うと、セルを
    選んだだけの状態で打ち始めた1文字目が確定前に取られ、
    「ぶ」が「bう」になるため"""
    ids_key = f"{prefix}__ids"
    ids = st.session_state[ids_key]

    h1, h2, _h3 = st.columns([1.2, 2.2, 0.4])
    h1.caption("商品名")
    h2.caption("その商品をおすすめする理由")

    for pid in ids:
        c1, c2, c3 = st.columns([1.2, 2.2, 0.4], vertical_alignment="bottom")
        c1.text_input("商品名", key=f"{prefix}__pname_{pid}",
                      label_visibility="collapsed", placeholder="甲州織の傘")
        c2.text_input("理由", key=f"{prefix}__preason_{pid}",
                      label_visibility="collapsed",
                      placeholder="骨を替えれば一生使える")
        if len(ids) > 1 and c3.button("✕", key=f"{prefix}__pdel_{pid}",
                                      help="この行を消す"):
            st.session_state[ids_key] = [x for x in ids if x != pid]
            st.rerun()

    if st.button("＋ 商品を追加", key=f"{prefix}__add"):
        nid = st.session_state[f"{prefix}__next"]
        st.session_state[f"{prefix}__next"] = nid + 1
        st.session_state[ids_key] = ids + [nid]
        st.rerun()


def _collect_products(prefix: str) -> list[dict]:
    out = []
    for pid in st.session_state.get(f"{prefix}__ids", []):
        nm = str(st.session_state.get(f"{prefix}__pname_{pid}") or "").strip()
        rs = str(st.session_state.get(f"{prefix}__preason_{pid}") or "").strip()
        if nm:
            out.append({"name": nm, "reason": rs})
    return out


def _clear_inputs(prefix: str) -> None:
    for k in [k for k in st.session_state if k.startswith(prefix + "__")]:
        st.session_state.pop(k, None)


def _form_prefix(layer: str) -> str:
    """入力欄のキーに版番号を混ぜる。キーを消すだけでは画面側が同じ値を
    送り直してしまい、登録後に欄が空にならないため"""
    return f"f_{layer}_{st.session_state.get(f'ver_{layer}', 0)}"


def _reset_form(layer: str, prefix: str) -> None:
    _clear_inputs(prefix)
    st.session_state[f"ver_{layer}"] = st.session_state.get(f"ver_{layer}", 0) + 1


def _has_input(prefix: str) -> bool:
    for suffix in ("name", "url", "reason"):
        if str(st.session_state.get(f"{prefix}__{suffix}") or "").strip():
            return True
    return bool(_collect_products(prefix))


def render_form(layer: str, curator: str) -> None:
    if layer == "known":
        st.info("**知っているブランドを登録します。** "
                "あなたが元から知っていて、贈り物としておすすめできるものを入れてください。")
    else:
        st.info("**調べていて見つけたブランドを登録します。** "
                "なぜピンときたのか、その理由がいちばん大事な情報になります。")

    flash = st.session_state.pop(f"flash_{layer}", None)
    if flash:
        st.success(flash)

    prefix = _form_prefix(layer)
    _seed_products(prefix)

    c1, c2 = st.columns([1, 1.4])
    with c1:
        st.text_input("ブランド名 *", key=f"{prefix}__name", placeholder="小宮商店")
    with c2:
        st.text_input("公式サイトのURL *", key=f"{prefix}__url",
                      placeholder="https://www.komiyakasa.jp/")

    st.text_area(
        "このブランドをおすすめする理由 *", height=110, key=f"{prefix}__reason",
        placeholder=("ホームページに書いてあることの写しではなく、あなたの言葉で。"
                     "\n例）修理しながら永く使うことを前提に作っている。"
                     "職人が一本ずつ手で仕上げていて、直して使う文化そのものを掲げている。"),
        help="ここが最も価値のある情報です。AIには書けない部分なので、"
             "思ったことをそのまま書いてください。")

    st.markdown("###### おすすめの商品（あれば・何件でも）")
    _product_inputs(prefix)
    st.caption("商品が思い当たらなければ、空欄のままで構いません。")

    name = st.session_state.get(f"{prefix}__name", "")
    url = st.session_state.get(f"{prefix}__url", "")
    brand_reason = st.session_state.get(f"{prefix}__reason", "")

    b1, b2 = st.columns([3, 1])
    submitted = b1.button("登録する", key=f"{prefix}__submit",
                          use_container_width=True, type="primary")
    if b2.button("入力を消す", key=f"{prefix}__clear", use_container_width=True):
        if _has_input(prefix):
            st.session_state[f"confirm_clear_{layer}"] = True
        else:
            _reset_form(layer, prefix)
        st.rerun()

    if st.session_state.get(f"confirm_clear_{layer}"):
        st.warning("入力中の内容を消します。元に戻せません。")
        y, n = st.columns(2)
        if y.button("消す", key=f"{prefix}__clear_yes", use_container_width=True):
            st.session_state.pop(f"confirm_clear_{layer}", None)
            _reset_form(layer, prefix)
            st.rerun()
        if n.button("やめる", key=f"{prefix}__clear_no", use_container_width=True):
            st.session_state.pop(f"confirm_clear_{layer}", None)
            st.rerun()

    if submitted:
        errs = check(name, url, brand_reason)
        if errs:
            for e in errs:
                st.error(e)
            return

        plist = _collect_products(prefix)

        dup = find_own_duplicate(url, curator)
        if dup:
            st.error(f"このURLは「{dup.get('brand_name')}」として登録済みです")
            return

        hit = find_existing(name, url)
        if hit:
            st.session_state[f"force_{layer}"] = {
                "brand_name": name.strip(), "official_url": url.strip(),
                "brand_reason": brand_reason.strip(), "products": plist,
                "layer": layer, "curator": curator,
                "existing_brand_id": hit.get("id"),
                "existing_name": hit.get("name"), "existing_kind": hit["kind"],
            }
            st.rerun()

        ok, msg = save_row({
            "brand_name": name.strip(),
            "official_url": url.strip(),
            "brand_reason": brand_reason.strip(),
            "products": plist,
            "layer": layer,
            "curator": curator,
        })
        if not ok:
            st.error(msg)
            return
        st.session_state[f"flash_{layer}"] = f"{name.strip()} を登録しました（{msg}）"
        _reset_form(layer, prefix)
        st.cache_data.clear()
        st.rerun()

    pending = st.session_state.get(f"force_{layer}")
    if pending:
        st.warning(
            f"**「{pending.get('existing_name')}」は既にブランドDBに入っています**"
            f"（{pending.get('existing_kind')}が一致）。"
            "　それでも登録すると、あなたの理由が既存のブランドに追記される形になります。"
            "重複ではなく理由を足したい場合は、そのまま登録して構いません。")
        c1, c2 = st.columns(2)
        if c1.button("それでも登録する", key=f"force_ok_{layer}", use_container_width=True):
            skip = ("existing_brand_id", "existing_name", "existing_kind")
            row = {k: v for k, v in pending.items() if k not in skip}
            row["review_notes"] = (
                f"既存ブランド {pending.get('existing_brand_id')} と重複の可能性")
            ok, msg = save_row(row)
            st.session_state.pop(f"force_{layer}", None)
            if ok:
                st.session_state[f"flash_{layer}"] = f"登録しました（{msg}）"
                _reset_form(layer, prefix)
                st.cache_data.clear()
            else:
                st.error(msg)
            st.rerun()
        if c2.button("やめる", key=f"force_no_{layer}", use_container_width=True):
            st.session_state.pop(f"force_{layer}", None)
            st.rerun()


def _products_of(row: dict) -> list[dict]:
    """新旧どちらの形でも、商品の一覧として取り出す"""
    ps = row.get("products") or []
    if not ps and row.get("product_name"):
        ps = [{"name": row["product_name"], "reason": row.get("product_reason") or ""}]
    return ps


def _excerpt(text: str, n: int = 22) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n] + "…"


def _render_delete(key, row: dict, curator: str) -> None:
    """一発では消さない。押し間違いは取り返しがつかないため"""
    flag = f"confirm_del_{key}"
    if not st.session_state.get(flag):
        if st.button("この登録を削除する", key=f"del_{key}"):
            st.session_state[flag] = True
            st.rerun()
        return

    st.warning(f"「{row.get('brand_name')}」を削除します。元に戻せません。")
    yes, no = st.columns(2)
    if yes.button("削除する", key=f"yes_{key}", use_container_width=True):
        delete_row(key, curator)
        st.session_state.pop(flag, None)
        st.session_state["edit_target"] = None
        st.cache_data.clear()
        st.rerun()
    if no.button("やめる", key=f"no_{key}", use_container_width=True):
        st.session_state.pop(flag, None)
        st.rerun()


def _render_editor(row: dict, key, products: list[dict], curator: str) -> None:
    prefix = f"e_{key}"
    _seed_products(prefix, products)
    if f"{prefix}__name" not in st.session_state:
        st.session_state[f"{prefix}__name"] = row.get("brand_name") or ""
        st.session_state[f"{prefix}__url"] = row.get("official_url") or ""
        st.session_state[f"{prefix}__reason"] = row.get("brand_reason") or ""
        st.session_state[f"{prefix}__layer"] = row.get("layer") or "known"

    with st.container(border=True):
        c1, c2 = st.columns([1, 1.4])
        with c1:
            st.text_input("ブランド名", key=f"{prefix}__name")
        with c2:
            st.text_input("公式サイトのURL", key=f"{prefix}__url")
        st.text_area("このブランドをおすすめする理由", height=120,
                     key=f"{prefix}__reason")

        st.markdown("###### おすすめの商品")
        _product_inputs(prefix)

        st.selectbox("どちらで登録したか", ["known", "explored"],
                     key=f"{prefix}__layer", format_func=lambda v: LAYERS[v])

        if st.button("この内容で更新する", key=f"{prefix}__save",
                     use_container_width=True, type="primary"):
            name = st.session_state.get(f"{prefix}__name", "")
            url = st.session_state.get(f"{prefix}__url", "")
            reason = st.session_state.get(f"{prefix}__reason", "")
            errs = check(name, url, reason)
            if errs:
                for x in errs:
                    st.error(x)
            else:
                ok, msg = update_row(key, curator, {
                    "brand_name": name.strip(),
                    "official_url": url.strip(),
                    "brand_reason": reason.strip(),
                    "products": _collect_products(prefix),
                    "layer": st.session_state.get(f"{prefix}__layer", "known"),
                })
                if ok:
                    _clear_inputs(prefix)
                    st.cache_data.clear()
                    st.session_state["edit_target"] = None
                    st.rerun()
                else:
                    st.error(msg)

        _render_delete(key, row, curator)


def render_list(curator: str) -> None:
    rows = load_rows(curator)
    if not rows:
        st.caption("まだ登録がありません。")
        return

    n_known = sum(1 for r in rows if r.get("layer") == "known")
    m1, m2, m3 = st.columns(3)
    m1.metric("登録数", len(rows))
    m2.metric("知っているもの", n_known)
    m3.metric("見つけたもの", len(rows) - n_known)
    st.markdown("---")

    widths = [3, 4.4, 0.9, 1.4]
    h1, h2, h3, _h4 = st.columns(widths)
    h1.caption("ブランド")
    h2.caption("おすすめの理由")
    h3.caption("商品")

    for row in rows:
        key = row.get("id") if isinstance(row.get("id"), int) else row.get("official_url")
        products = _products_of(row)
        opened = st.session_state.get("edit_target") == key

        c1, c2, c3, c4 = st.columns(widths, vertical_alignment="center")
        c1.markdown(f"**{row.get('brand_name')}**")
        c2.markdown(
            "<div style='color:#8b93a1;white-space:nowrap;overflow:hidden;"
            "text-overflow:ellipsis'>"
            f"{_excerpt(row.get('brand_reason') or '')}</div>",
            unsafe_allow_html=True)
        c3.markdown(
            "<div style='color:#8b93a1'>"
            f"{str(len(products)) + '点' if products else '—'}</div>",
            unsafe_allow_html=True)
        if c4.button("閉じる" if opened else "編集", key=f"tgl_{key}",
                     use_container_width=True):
            st.session_state["edit_target"] = None if opened else key
            st.rerun()

        if opened:
            _render_editor(row, key, products, curator)
        st.markdown(
            "<hr style='margin:0.4rem 0;border:none;border-top:1px solid #303643'>",
            unsafe_allow_html=True)


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

    ok, why = _connection_status()
    if not ok:
        if _on_cloud():
            st.error("**データベースに繋がっていません。このまま入力しないでください。**")
            st.error(f"理由: {why}")
            st.caption("この状態で登録すると、入力が保存されないまま消えることがあります。"
                       "管理者に連絡してください。")
            st.stop()
        st.warning(f"データベースに繋がっていません（{why}）。"
                   "この端末に保存するので、そのまま入力を続けて構いません。")

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

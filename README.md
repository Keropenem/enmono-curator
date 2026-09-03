# enmono キュレーター入力アプリ

ギフト提案サービスのブランドDBに、推薦ブランドを登録するための画面と、既存DBへ合流させるための一式。

## 何を集めているか

入力は4項目だけにしている。

- ブランド名
- 公式サイトのURL
- そのブランドをおすすめする理由
- おすすめの商品（商品名・商品ページのURL・その商品をおすすめする理由）。何点でも

カテゴリ・価格帯・タグは入力しない。公式サイトを見れば分かることは後段で機械的に補うため、ここでは人にしか書けない推薦理由だけを集める。

## データの置き場所

Supabase の `curator_picks_sandbox`。既存の `brands_sandbox` とは分けている。人が書いたものとAIが埋めたものを混ぜないため。

| カラム | 中身 |
|---|---|
| `brand_name` | ブランド名 |
| `official_url` | 公式サイトのURL |
| `brand_reason` | おすすめする理由。入力者の言葉をそのまま置く |
| `products` | jsonb の配列。1件ごとに `name` / `url` / `reason` |
| `product_name` `product_reason` | 旧カラム。`products` の先頭1点の写し |
| `layer` | `known`（元から知っている） / `explored`（調べて出会った） |
| `curator` | 入力者 |
| `status` | `submitted` → `reviewed` / `promoted` / `rejected` |
| `promoted_brand_id` | 昇格先の `brands_sandbox.id` |

`UNIQUE (curator, official_url)` で、同じ人が同じURLを二重に登録できないようにしている。

**商品の正は `products`。** `product_name` / `product_reason` は過去データと並べて見るために画面が自動で写しているだけで、直接書いても次の更新で上書きされる。読むときも `products` を見る。

## 既存ブランドとの突き合わせ

`curator_picks_with_match` ビューが、`normalize_url()` で `https://`・`www.`・末尾スラッシュを落としてから `brand_urls_sandbox` と照合し、URL一致と名前一致を `match_kind` で区別して返す。登録画面の重複警告はこれを引いている。同じ判定が要るときはテーブルではなくこのビューを使う。

件数の推移は `curator_picks_progress` で見る。

## 既存DBへの合流

自動では流れない。溜まってから `scripts/promote_curator_picks.py` を手で回す。

```bash
python scripts/promote_curator_picks.py --dry-run
python scripts/promote_curator_picks.py
```

重複チェック、AIが公式サイトから哲学・カテゴリ・価格帯を補完、`brands_sandbox` に `status='draft'` で投入、という順。draft なのでレビュー画面を通してから active になる。登録のたびに流すと補完の失敗がそのまま本番に混ざるため、この形にしている。

| 入力側 | 既存DB側 |
|---|---|
| `brand_name` | `name_ja` |
| `official_url` | `brand_urls_sandbox.official_url` |
| `brand_reason` | `curator_rationale` |
| `curator` | `curator_added_by` |
| `layer` | `curator_source` |

`brand_reason` は要約も書き換えもせずそのまま入れる。AIに書けないのはここだけなので、加工すると集めた意味がなくなる。商品名とURLも同じ欄に連ねて残す。

## 起動

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## 設定

Streamlit Community Cloud では **Secrets**、手元では `.env.local` に置く。

| キー | 用途 |
|---|---|
| `SUPABASE_URL` | 接続先。プロジェクト固有の20文字の識別子であって、アプリ名ではない |
| `SUPABASE_SERVICE_ROLE_KEY` | 接続キー |
| `CURATOR_PASSWORD` | 画面を開くための合言葉 |
| `CURATOR_NAME` | 登録者の識別子 |
| `GEMINI_API_KEY` | 合流スクリプトの補完に使う。画面側では使わない |

画面はサービスロールキーで書き込むので、このテーブルに RLS を張る場合は画面側の挙動も一緒に確認すること。

手元で接続情報が無い場合は端末内のファイルに保存され、あとから書き出せる。Streamlit Cloud で繋がらない場合は、黙って端末保存に落ちると入力が消えるため、画面を止めて理由を出す。

## ファイル

| パス | 中身 |
|---|---|
| `streamlit_app.py` | 入力画面 |
| `sql/010_curator_picks.sql` | テーブル・ビュー・正規化関数 |
| `sql/011_curator_products.sql` | 商品を配列で持つための追加 |
| `scripts/promote_curator_picks.py` | 既存DBへの合流 |
| `docs/curator_guide.md` | 入力者向けの手引き |

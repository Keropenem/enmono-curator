# enmono キュレーター入力アプリ

ギフト提案サービスのブランドDBに、推薦ブランドを登録するための画面。

入力するのは4項目のみ。

- ブランド名
- 公式サイトのURL
- そのブランドをおすすめする理由
- おすすめの商品とその理由（何点でも）

カテゴリ・価格帯・タグは入力しない。公式サイトから機械的に取得できる情報は後段で補うため、ここでは人にしか書けない推薦理由だけを集める。

登録時に既存ブランドとの重複を判定し、URLの表記ゆれ（`www` の有無、末尾スラッシュ）を吸収して照合する。

登録済みのものは「登録した一覧」タブから編集・削除できる。

## 起動

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## 設定

Streamlit Community Cloud では **Secrets**、手元では `.env.local` に置く。

| キー | 用途 |
|---|---|
| `SUPABASE_URL` | 接続先 |
| `SUPABASE_SERVICE_ROLE_KEY` | 接続キー |
| `CURATOR_PASSWORD` | 画面を開くための合言葉 |
| `CURATOR_NAME` | 登録者の識別子 |

未設定の場合は端末内のファイルに保存され、あとから書き出せる。

## 保存先

`curator_picks_sandbox` テーブル。データの整備は別リポジトリで行う。

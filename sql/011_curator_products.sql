-- おすすめ商品を複数登録できるようにする
-- 会議後の要望 (2026-09-02): 1ブランドに複数の推薦商品を紐づけたい
--
-- 別テーブルにせず jsonb 配列で持つ理由:
--   商品は「そのブランドを推す理由の一部」であり、単独で検索・集計する対象ではない。
--   件数も1ブランドあたり数点にとどまるため、テーブルを分ける利点が薄い。
--   後段で products_sandbox を使う判断になったときも、この配列から展開できる。

ALTER TABLE public.curator_picks_sandbox
  ADD COLUMN IF NOT EXISTS products jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN public.curator_picks_sandbox.products IS
  'おすすめ商品の配列。[{"name": "折りたたみ傘", "reason": "この一本に考えが出ている"}, ...]';

-- 既存の単一商品カラムがあれば配列へ移す（0件でも安全に流せる）
UPDATE public.curator_picks_sandbox
SET products = jsonb_build_array(
      jsonb_build_object('name', product_name, 'reason', coalesce(product_reason, '')))
WHERE product_name IS NOT NULL
  AND product_name <> ''
  AND products = '[]'::jsonb;

-- 商品名で探せるようにしておく
CREATE INDEX IF NOT EXISTS idx_curator_picks_products
  ON public.curator_picks_sandbox USING gin (products);

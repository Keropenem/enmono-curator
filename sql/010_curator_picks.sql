-- キュレーター入力テーブル
-- 会議 (2026-09-01) の決定を反映:
--   - 入力は4項目のみ: ブランド名 / 公式URL / おすすめ理由 / おすすめ商品とその理由
--   - タグは入力させない。AI がクローリングした方が粒度が揃い、入力者に迷わせる時間がもったいない
--   - URL さえあれば AI が引ける情報は入力させない。AI が引けない「目利きの理由」だけを集める
--   - 2 段階のレイヤーで集める:
--       known    = 元から知っていて、おすすめできるブランド
--       explored = 調べていて出会い、ピンときたブランド（なぜピンときたかが重要）
--   - brands_sandbox とは分離する。生入力を貯め、後段の AI 拡張で brands_sandbox へ昇格させる

CREATE TABLE IF NOT EXISTS public.curator_picks_sandbox (
  id                serial PRIMARY KEY,

  -- 入力4項目
  brand_name        text NOT NULL,
  official_url      text NOT NULL,
  brand_reason      text,                    -- このブランドをおすすめする理由
  product_name      text,                    -- おすすめの商品
  product_reason    text,                    -- その商品をおすすめする理由

  -- 収集レイヤー
  layer             text NOT NULL DEFAULT 'known'
                    CHECK (layer IN ('known', 'explored')),

  -- 運用メタ
  curator           text NOT NULL DEFAULT 'tsutsumi',
  status            text NOT NULL DEFAULT 'submitted'
                    CHECK (status IN ('submitted', 'reviewed', 'promoted', 'rejected')),
  review_notes      text,                    -- レビュー側のメモ
  promoted_brand_id text REFERENCES public.brands_sandbox(id) ON DELETE SET NULL,

  created_at        timestamptz DEFAULT now(),
  updated_at        timestamptz DEFAULT now(),

  -- 同じキュレーターが同じURLを二重登録しないようにする
  UNIQUE (curator, official_url)
);

CREATE INDEX IF NOT EXISTS idx_curator_picks_curator_created
  ON public.curator_picks_sandbox (curator, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_curator_picks_status
  ON public.curator_picks_sandbox (status);
CREATE INDEX IF NOT EXISTS idx_curator_picks_layer
  ON public.curator_picks_sandbox (layer);

CREATE OR REPLACE FUNCTION public.tg_curator_picks_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_curator_picks_updated_at ON public.curator_picks_sandbox;
CREATE TRIGGER trg_curator_picks_updated_at
  BEFORE UPDATE ON public.curator_picks_sandbox
  FOR EACH ROW EXECUTE FUNCTION public.tg_curator_picks_updated_at();

COMMENT ON TABLE public.curator_picks_sandbox IS
  'キュレーターの生入力。4項目のみ。AI 拡張前の一次データで、brands_sandbox の上流にあたる';
COMMENT ON COLUMN public.curator_picks_sandbox.layer IS
  'known = 元から知っている / explored = 調べて出会い、ピンときた';

-- =====================================================================
-- 既存ブランドDBへの接続
-- =====================================================================
-- 昇格の経路:
--   curator_picks_sandbox (4項目の生入力)
--     → scripts/promote_curator_picks.py が URL から情報を補完
--     → brands_sandbox (status='draft') + brand_urls_sandbox に投入
--     → 既存の draft レビュー画面で active へ昇格
--
-- brands_sandbox 側の受け皿（既存カラムをそのまま使う）:
--   brand_name     → name_ja
--   official_url   → brand_urls_sandbox.official_url
--   brand_reason   → curator_rationale       ※AIが書けない一次情報。そのまま残す
--   curator        → curator_added_by
--   layer          → curator_source ('curator_known' / 'curator_explored')
--   product_*      → recipient_fit_freetext の材料。商品DBを作る場合は products_sandbox へ
--
-- philosophy_one_liner / philosophy_long / price_tiers / main_categories などは
-- 堤さんに入力させない方針のため、昇格スクリプトが URL から補完する。
-- 補完前は status='draft' で置き、validate.py の active チェックにはかけない。

-- URL の表記ゆれを吸収して既存ブランドと突き合わせるための正規化
CREATE OR REPLACE FUNCTION public.normalize_url(u text) RETURNS text AS $$
  SELECT regexp_replace(
           regexp_replace(
             regexp_replace(lower(coalesce(u, '')), '^https?://', ''),
             '^www\.', ''),
           '/+$', '');
$$ LANGUAGE sql IMMUTABLE;

-- 既存ブランドとの重複を検知するビュー（入力画面がこれを引いて警告を出す）
CREATE OR REPLACE VIEW public.curator_picks_with_match AS
SELECT
  p.*,
  b.id                                  AS matched_brand_id,
  b.name_ja                             AS matched_brand_name,
  CASE
    WHEN b.id IS NOT NULL THEN 'url'
    WHEN bn.id IS NOT NULL THEN 'name'
    ELSE NULL
  END                                   AS match_kind,
  COALESCE(b.id, bn.id)                 AS existing_brand_id,
  COALESCE(b.name_ja, bn.name_ja)       AS existing_brand_name
FROM public.curator_picks_sandbox p
LEFT JOIN public.brand_urls_sandbox u
       ON public.normalize_url(u.official_url) = public.normalize_url(p.official_url)
LEFT JOIN public.brands_sandbox b ON b.id = u.brand_id
LEFT JOIN public.brands_sandbox bn ON bn.name_ja = p.brand_name;

COMMENT ON VIEW public.curator_picks_with_match IS
  'キュレーター入力に、既存 brands_sandbox との突き合わせ結果を付けたビュー。重複登録の検知に使う';

-- 昇格の進み具合を見るビュー
CREATE OR REPLACE VIEW public.curator_picks_progress AS
SELECT curator, layer, status, count(*) AS n,
       min(created_at) AS first_at, max(created_at) AS last_at
FROM public.curator_picks_sandbox
GROUP BY curator, layer, status;

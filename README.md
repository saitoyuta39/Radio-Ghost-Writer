# Radio Ghost Writer

リファレンス記事（ラジオ原稿など）のスタイルを分析し、同じトーンで新しい記事を生成する執筆支援ツール。
AIには Claude (Anthropic) および GPT-4o-mini (OpenAI) を使用します。

## 主な機能

1.  **スタイル分析** ── 過去の原稿から構成・文体・リズムを分析。
2.  **インタビュー** ── AIが素材を深掘りし、内容を具体化。
3.  **記事生成** ── 分析したスタイルを再現してラジオ原稿を作成。
4.  **プロンプト管理** ── ブラウザ上でAIの指示（プロンプト）を直接調整可能。
5.  **Google Drive連携** ── 生成した原稿をMarkdown形式で直接ドライブへ保存。

## セットアップ（ローカル開発）

```bash
# 依存関係のインストール
pip3 install -r requirements.txt

# 環境変数の設定
cp .env.example .env
# .env ファイルを開いて必要なAPIキーを設定

# 起動
python app.py
```
ブラウザで `http://localhost:5050` を開きます。

## Vercel へのデプロイ

1.  GitHubリポジトリをVercelにインポートします。
2.  以下の環境変数をVercelのプロジェクト設定で登録します。
    - `ANTHROPIC_API_KEY`
    - `OPENAI_API_KEY`
    - `SUPABASE_URL` / `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_ROLE_KEY` / `SUPABASE_JWT_SECRET`
    - `SPREADSHEET_ID` / `GOOGLE_DRIVE_FOLDER_ID`
    - `GCP_SERVICE_ACCOUNT_JSON` （`service_account.json` の中身をそのまま貼り付け）

## Supabase の初期設定

Supabaseの SQL Editor で以下のSQLを実行し、必要なテーブルを作成してください。

```sql
-- 記事（ラジオ原稿）保存用
CREATE TABLE IF NOT EXISTS public.articles (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id uuid NOT NULL,
    title text,
    question text,
    html text,
    text_content text,
    memo text,
    conversation jsonb,
    context_id uuid,
    status text DEFAULT 'draft',
    created_at timestamp with time zone DEFAULT now()
);

-- コンテキスト（リファレンススタイル）保存用
CREATE TABLE IF NOT EXISTS public.contexts (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id uuid NOT NULL,
    name text,
    reference_texts text[],
    style_guide jsonb,
    created_at timestamp with time zone DEFAULT now()
);

-- カスタムプロンプト保存用（Vercel上での編集用）
CREATE TABLE IF NOT EXISTS public.custom_prompts (
    path text PRIMARY KEY,
    content text NOT NULL,
    updated_at timestamp with time zone DEFAULT now()
);

-- RLS設定（必要に応じて適宜調整してください）
ALTER TABLE public.articles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.contexts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.custom_prompts ENABLE ROW LEVEL SECURITY;

-- 全アクセス許可のポリシー（開発用。運用に合わせて auth.uid() = user_id などに制限してください）
CREATE POLICY "Enable all for articles" ON public.articles FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Enable all for contexts" ON public.contexts FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role access" ON public.custom_prompts FOR ALL TO service_role USING (true) WITH CHECK (true);
```

## プロンプトの優先順位

プロンプトは以下の優先順位で読み込まれます：
1.  **Supabase (`custom_prompts` テーブル)** ── ブラウザで保存した最新のプロンプト
2.  **`prompts/` フォルダ** ── デプロイ時に同梱されている原本ファイル

リセットボタンを押すと、DBから設定が削除され、`prompts/` フォルダの内容に戻ります。

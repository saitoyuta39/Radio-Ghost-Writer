import os
import re
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

def fetch_general_url(url):
    """一般のウェブサイト（WordPress等）から記事本文とメタ情報を取得する"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"

        soup = BeautifulSoup(resp.text, "html.parser")

        # 不要なタグを除去
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe", "noscript", "svg", "canvas", "audio", "video"]):
            tag.decompose()

        # 特定のクラスやIDを持つ要素を除去
        unwanted_selectors = [
            ".toc_container", "#toc_container", ".ez-toc-container", ".wp-block-table-of-contents",
            ".entry-meta", ".post-meta", ".author-info", ".cat-links", ".tags-links", ".posted-on",
            ".sharedaddy", ".jp-relatedposts", ".yarpp-related", ".wp-show-posts", ".social-share",
            ".post-navigation", ".navigation", ".pagination", ".comment-respond", ".comments-area",
            ".wp-block-buttons", ".wp-block-button", ".wp-block-audio", ".wp-audio-shortcode",
            ".related-posts", ".widget", ".sidebar", ".ad-container", ".adsbygoogle",
            ".wp-block-separator", ".wp-block-spacer", ".post-footer", ".entry-footer",
            ".entry-header", ".post-header", ".entry-utility", ".author-bio", ".rating-stars",
            ".wp-block-post-date", ".wp-block-post-author", ".wp-block-post-excerpt",
            ".wp-block-post-navigation-link", ".wp-block-query-pagination",
            ".post-tags", ".taxonomy-description", ".post-categories", ".entry-categories", ".entry-tags"
        ]
        for selector in unwanted_selectors:
            for tag in soup.select(selector):
                tag.decompose()

        # 記事本文と思われる箇所を特定
        article_selectors = [
            "article", "main", 
            ".post-content", ".entry-content", ".article-body", ".content-body",
            "#content", ".main-content"
        ]
        body = None
        for sel in article_selectors:
            body = soup.select_one(sel)
            if body:
                break
        
        if not body:
            body = soup.body

        # 特定のテキストを含む要素を削除
        if body:
            # タイトル（h1）が本文の最初にある場合は削除（別途metaとして取得しているため）
            # ただし、body直下の最初の要素に近い場合のみ
            first_h1 = body.find("h1")
            if first_h1:
                # 念のため、h1が非常に長い（本文がh1に入っている）場合は消さない
                if len(first_h1.get_text(strip=True)) < 200:
                    first_h1.decompose()

            # 削除対象のキーワード
            unwanted_keywords = [
                "AI音声による読み上げ", "違和感があるかもしれませんが", 
                "更新のペースを上げて", "楽しみに待っていてください",
                "この記事はいかがでしたか", "聞いたラジオ", "お名前（ひらがな）"
            ]

            # 子要素を精査して削除
            for tag in body.find_all(["p", "span", "h1", "h2", "h3", "h4", "h5", "h6", "li", "div"]):
                # divの場合は、そのタグ自体が非常に長いテキストを持っている場合は、
                # コンテナの可能性が高いので、中身を個別にチェックさせるためにスキップ
                if tag.name == "div" and len(tag.get_text(strip=True)) > 500:
                    continue

                txt = tag.get_text(strip=True)
                
                # 完全一致または特定の記号
                if txt in ["目次", "⬅︎", "☆", "※"]:
                    tag.decompose()
                    continue

                # プレイヤー操作系
                if re.match(r"^\d{2}:\d{2}$", txt) or re.match(r"^\d+×$", txt):
                    tag.decompose()
                    continue

                # 部分一致での削除（タグが短い場合のみ実行して、本文巻き込みを防ぐ）
                if len(txt) < 300:
                    for kw in unwanted_keywords:
                        if kw in txt:
                            tag.decompose()
                            break

        # タイトル取得
        title = ""
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = og_title["content"]
        else:
            title_tag = soup.find("title")
            if title_tag:
                title = title_tag.get_text(strip=True)

        # テキスト抽出
        text = body.get_text("\n", strip=True) if body else ""
        
        # 不要なパターンの最終的なクリーニング（行単位）
        cleaned_lines = []
        
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            
            # カテゴリー一覧のような行を除外（短い単語が並んでいる、または特定の単語を含む）
            if line in ["SnsClubラジオ", "マインドセット・自己成長", "SNS運用・マーケティング", "コミュニティ・サービス（SnsClub／エスキャン）", "ライフハック・生産性・健康", "ストーリー・経験談・考察", "ツール・機材・テクノロジー系", "Mosseri NEWS", "SnsClub NEXT", "ストーリー", 
                       "成功／失敗ストーリー", "モチベーション・目標設定", "人間関係・環境", "習慣・時間管理",
                       "リール", "万垢達成者インタビュー"]:
                continue

            # 日付パターンを除外
            if (re.match(r"^\d{4}[\s年/]\d{1,2}[/月]\d{1,2}日?$", line) or 
                re.match(r"^\d{1,2}/\d{1,2}$", line) or
                re.match(r"^\d{4}$", line)): # 4桁の数字のみ（年）を除外
                continue
            
            # 特定の記号のみの行を除外
            if line in ["☆", "※", "⬅︎", "★", "■", "●"]:
                continue

            # オープニングや挨拶より前のメタ情報は除外したい
            # ただし、タイトルなどはすでに除外されているはず
            
            cleaned_lines.append(line)
        
        text = "\n".join(cleaned_lines)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        return {
            "text": text,
            "meta": {
                "article_title": title,
                "author": "",
                "display_name": "",
                "username": "",
                "source_type": "web"
            }
        }
    except Exception as e:
        raise ValueError(f"URLからの取得に失敗しました: {str(e)}")

def is_youtube_url(url):
    """YouTube URLかどうか判定する（互換性のために残す）"""
    return bool(re.match(
        r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/", url
    ))

def fetch_sync(url):
    """同期ラッパー。現在はWordPressを含む一般サイトのみに対応"""
    return fetch_general_url(url)

if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://wordpress.org/news/2024/02/the-month-in-wordpress-january-2024/"
    try:
        result = fetch_sync(url)
        print(f"TITLE: {result['meta'].get('article_title', '')}")
        print(f"LENGTH: {len(result['text'])}")
        print("---CONTENT---")
        print(result["text"][:500] + "...")
    except Exception as e:
        print(f"ERROR: {e}")

"""
Робота з WordPress REST API через Application Passwords.
ВАЖЛИВО: цей модуль НІКОЛИ не публікує контент напряму — create_draft
завжди створює запис зі статусом "draft". Публікація лишається за людиною
у wp-admin. Це і є той самий захист "не зламати сайт".
"""

import requests


class WordPressClient:
    def __init__(self, base_url: str, username: str, app_password: str):
        self.base_url = base_url.rstrip("/")
        self.auth = (username, app_password)

    def _get(self, path: str, params: dict | None = None):
        resp = requests.get(
            f"{self.base_url}/wp-json/wp/v2/{path}",
            params=params or {},
            auth=self.auth,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict):
        resp = requests.post(
            f"{self.base_url}/wp-json/wp/v2/{path}",
            json=payload,
            auth=self.auth,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def list_content(self, post_type: str = "posts", per_page: int = 50) -> list[dict]:
        """Короткий список (id, title, slug, link) — для огляду структури сайту."""
        items = self._get(post_type, {"per_page": per_page, "status": "publish,draft"})
        return [
            {
                "id": item["id"],
                "title": item["title"]["rendered"],
                "slug": item["slug"],
                "link": item["link"],
            }
            for item in items
        ]

    def search_content(self, query: str, post_type: str = "posts") -> list[dict]:
        """Пошук схожих за змістом сторінок/постів — щоб знайти зразок дизайну."""
        items = self._get(post_type, {"search": query, "per_page": 5})
        return [
            {"id": i["id"], "title": i["title"]["rendered"], "slug": i["slug"]}
            for i in items
        ]

    def get_raw_content(self, post_id: int, post_type: str = "posts") -> str:
        """Повертає Gutenberg/HTML-розмітку запису — саме її копіює стиль агент-виконавець."""
        item = self._get(f"{post_type}/{post_id}", {"context": "edit"})
        return item["content"]["raw"]

    def create_draft(self, title: str, content: str, post_type: str = "posts") -> dict:
        """Для НОВОГО контенту, якого ще не існує на сайті — створює запис
        зі статусом draft (це безпечно, бо живої версії ще немає)."""
        result = self._post(post_type, {
            "title": title,
            "content": content,
            "status": "draft",
        })
        return {
            "id": result["id"],
            "edit_link": f"{self.base_url}/wp-admin/post.php?post={result['id']}&action=edit",
        }

    def _fetch_seo_tags(self, url: str) -> dict:
        """Витягує <title> і <meta name=description> з реального HTML сторінки."""
        from bs4 import BeautifulSoup
        try:
            resp = requests.get(url, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            title_tag = soup.find("title")
            desc_tag = soup.find("meta", attrs={"name": "description"})
            return {
                "seo_title": title_tag.get_text(strip=True) if title_tag else "",
                "meta_description": desc_tag.get("content", "") if desc_tag else "",
            }
        except Exception:
            return {"seo_title": "", "meta_description": ""}

    def get_page_snapshot(self, slug: str) -> dict | None:
        """Повертає title, meta description і текстовий вміст сторінки за slug.
        Шукає спочатку в pages, потім у posts."""
        from bs4 import BeautifulSoup
        for post_type in ("pages", "posts"):
            items = self._get(post_type, {"slug": slug})
            if not items:
                continue
            item = items[0]
            raw_html = item["content"].get("rendered", "")
            soup = BeautifulSoup(raw_html, "html.parser")
            text = " ".join(soup.get_text(" ", strip=True).split())[:3000]
            yoast = item.get("yoast_head_json") or {}
            return {
                "title": item["title"].get("rendered", ""),
                "meta_description": yoast.get("description", ""),
                "seo_title": yoast.get("title", ""),
                "text_content": text,
            }
        return None

    def find_by_slug(self, slug: str, post_type: str = "posts") -> dict | None:
        """Знаходить ОПУБЛІКОВАНИЙ запис за slug (останнім сегментом URL)."""
        items = self._get(post_type, {"slug": slug})
        return items[0] if items else None

    def propose_revision(self, post_id: int, content: str, post_type: str = "posts") -> dict:
        """Для ВЖЕ ОПУБЛІКОВАНОЇ сторінки: НЕ змінює статус і НЕ чіпає живий
        контент. Натомість створює автозбереження (autosave/revision),
        прикріплене до цього запису — точно так само, як WordPress робить
        це сам, коли ти редагуєш сторінку в редакторі, але ще не натиснув
        "Оновити". Жива сторінка лишається незмінною, доки людина сама
        не відкриє редактор і не підтвердить зміну."""
        result = self._post(f"{post_type}/{post_id}/autosaves", {"content": content})
        return {
            "id": result["id"],
            "edit_link": f"{self.base_url}/wp-admin/post.php?post={post_id}&action=edit",
        }

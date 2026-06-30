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
        items = self._get(post_type, {"per_page": per_page, "status": "publish"})
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
        items = self._get(post_type, {"search": query, "per_page": 5})
        return [
            {"id": i["id"], "title": i["title"]["rendered"], "slug": i["slug"]}
            for i in items
        ]

    def get_raw_content(self, post_id: int, post_type: str = "posts") -> str:
        try:
            item = self._get(f"{post_type}/{post_id}", {"context": "edit"})
            return item["content"]["raw"]
        except Exception:
            item = self._get(f"{post_type}/{post_id}")
            return item["content"]["rendered"]

    def create_draft(self, title: str, content: str, post_type: str = "posts") -> dict:
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
        items = self._get(post_type, {"slug": slug})
        return items[0] if items else None

    def find_block_on_site(self, block_hints: list[str]) -> str | None:
        """
        Сканує всі опубліковані пости і сторінки, знаходить перший Gutenberg-блок
        що відповідає підказкам (наприклад ['wp:uagb/faq', 'wp:yoast/faq-block']).
        Повертає чистий markup блоку від <!-- wp:... --> до <!-- /wp:... -->.
        """
        for post_type in ("posts", "pages"):
            try:
                items = self._get(post_type, {"per_page": 50, "status": "publish", "context": "edit"})
            except Exception:
                continue
            for item in items:
                content = item.get("content", {}).get("raw", "")
                if not content:
                    continue
                for hint in block_hints:
                    start_marker = f"<!-- {hint}"
                    # закриваючий тег — перше слово після wp:
                    block_name = hint.split()[0]
                    end_marker = f"<!-- /{block_name} -->"
                    start = content.find(start_marker)
                    if start == -1:
                        continue
                    end = content.find(end_marker, start)
                    if end != -1:
                        return content[start:end + len(end_marker)]
                    # self-closing block (без вкладеного контенту)
                    end_sc = content.find("-->", start)
                    if end_sc != -1:
                        return content[start:end_sc + 3]
        return None

    def find_best_template(self, rec_title: str, rec_description: str, fallback_id: int = 1751) -> tuple[int, str]:
        """
        Знаходить найкращий пост-шаблон серед опублікованих.
        Повертає (post_id, тип шаблону).
        """
        HAS_FAQ = "wp:yoast/faq-block"
        HAS_LIST = "wp:list"

        hint = (rec_title + " " + rec_description).lower()
        wants_faq = any(w in hint for w in ["faq", "питань", "запитань", "відповід"])
        wants_list = any(w in hint for w in ["список", "перелік", "кроки", "пункти", "етапи"])

        try:
            posts = self._get("posts", {"per_page": 50, "status": "publish", "context": "edit"})
        except Exception:
            return fallback_id, "standard"

        faq_candidates, list_candidates, standard_candidates = [], [], []

        for post in posts:
            content = post.get("content", {}).get("raw", "")
            if not content:
                continue
            pid = post["id"]
            if pid == fallback_id:
                continue
            if HAS_FAQ in content:
                faq_candidates.append(pid)
            elif HAS_LIST in content and len(content) > 2000:
                list_candidates.append(pid)
            elif len(content) > 2000:
                standard_candidates.append(pid)

        if wants_faq and faq_candidates:
            return faq_candidates[0], "faq"
        if wants_list and list_candidates:
            return list_candidates[0], "list"
        if standard_candidates:
            return standard_candidates[0], "standard"
        return fallback_id, "standard"

    def propose_revision(self, post_id: int, content: str, post_type: str = "posts") -> dict:
        """Для ВЖЕ ОПУБЛІКОВАНОЇ сторінки — створює autosave-ревізію.
        Якщо autosave недоступний (401) — створює чернетку з тим самим контентом."""
        try:
            result = self._post(f"{post_type}/{post_id}/autosaves", {"content": content})
            return {
                "id": result["id"],
                "edit_link": f"{self.base_url}/wp-admin/post.php?post={post_id}&action=edit",
            }
        except Exception:
            # Fallback: створити чернетку якщо autosave недоступний
            item = self._get(f"{post_type}/{post_id}")
            title = item.get("title", {}).get("rendered", "Без назви")
            result = self._post(post_type, {"title": f"[ПРАВКА] {title}", "content": content, "status": "draft"})
            return {
                "id": result["id"],
                "edit_link": f"{self.base_url}/wp-admin/post.php?post={result['id']}&action=edit",
            }

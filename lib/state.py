"""
Просте збереження стану між запусками у вигляді JSON-файлів.
Файли лежать у data/ і коммітяться назад у репозиторій GitHub Actions'ом
(див. .github/workflows/*.yml) — це і є "пам'ять" агента між запусками.
"""

import json
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def _path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def load_json(name: str, default):
    path = _path(name)
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return default
        return json.loads(content)


def save_json(name: str, data) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _path(name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

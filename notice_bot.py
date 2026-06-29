import os
import json
import time
import logging
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

TARGET_URLS = {
    "일반공지": "https://eulji.ac.kr/renewal/index.html?menuno=3160",
    "학사공지": "https://eulji.ac.kr/renewal/index.html?menuno=4005",
}

DB_FILE = "latest_notices.json"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK")

CATEGORY_COLORS = {
    "일반공지": 3447003,   # 파랑
    "학사공지": 15105570,  # 주황
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

MAX_SAVED_LINKS = 50   # 카테고리당 최대 저장 링크 수
REQUEST_TIMEOUT = 15   # 초


def get_notices(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")

        rows = soup.select("table.board-list tbody tr")
        notices = []

        for row in rows:
            if row.select_one("td.notice"):
                continue

            title_el = row.select_one("td.subject a")
            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            # urljoin으로 상대경로를 안전하게 절대경로로 변환
            link = urljoin(url, href)

            notices.append({"title": title, "link": link})

        return notices
    except requests.RequestException as e:
        log.error("페이지 요청 실패 (%s): %s", url, e)
        return []


def send_discord_message(category, title, link):
    if not DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK 환경 변수가 설정되지 않았습니다.")
        return False

    payload = {
        "embeds": [
            {
                "title": f"🔔 을지대학교 [{category}] 새 공지",
                "description": title,
                "url": link,
                "color": CATEGORY_COLORS.get(category, 3447003),
            }
        ]
    }

    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("디스코드 전송 실패 (%s): %s", category, e)
        return False


def load_history():
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("히스토리 파일 읽기 실패, 초기화합니다: %s", e)
        return {}


def save_history(history):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=4)
    except OSError as e:
        log.error("히스토리 파일 저장 실패: %s", e)


def main():
    history = load_history()

    for category, url in TARGET_URLS.items():
        notices = get_notices(url)
        if not notices:
            log.warning("[%s] 공지를 가져오지 못했습니다.", category)
            continue

        seen_links: list = history.get(category, [])

        # seen_links가 이전 버전의 문자열(str)이면 리스트로 마이그레이션
        if isinstance(seen_links, str):
            seen_links = [seen_links]

        seen_set = set(seen_links)
        new_notices = [n for n in notices if n["link"] not in seen_set]

        if not seen_links:
            log.info("[%s] 최초 실행: 현재 공지 %d건을 등록합니다.", category, len(notices))
        elif new_notices:
            # 오래된 것부터 먼저 보내기 위해 역순 전송
            for notice in reversed(new_notices):
                log.info("[%s] 새 공지 발견 → %s", category, notice["title"])
                send_discord_message(category, notice["title"], notice["link"])
                time.sleep(1)  # Discord 레이트 리밋 방지
        else:
            log.info("[%s] 새 공지 없음.", category)

        # 새로 본 링크를 앞에 추가하고 최대 개수 유지
        new_links = [n["link"] for n in notices]
        merged = new_links + [l for l in seen_links if l not in set(new_links)]
        history[category] = merged[:MAX_SAVED_LINKS]

    save_history(history)


if __name__ == "__main__":
    main()

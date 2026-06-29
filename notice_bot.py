import os
import re
import json
import time
import logging
import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
logging.getLogger("urllib3").setLevel(logging.ERROR)

TARGET_URLS = {
    "일반공지": "https://eulji.ac.kr/renewal/index.html?menuno=3160",
    "학사공지": "https://eulji.ac.kr/renewal/index.html?menuno=4005",
}

DB_FILE = "latest_notices.json"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK")

CATEGORY_COLORS = {
    "일반공지": 3447003,
    "학사공지": 15105570,
}

MAX_SAVED_LINKS = 50
REQUEST_TIMEOUT = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def get_notices(session, url):
    menuno = re.search(r"menuno=(\d+)", url).group(1)

    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")

        notices = []
        for row in soup.select("table.board-list tbody tr"):
            if row.select_one('td img[src*="icon_notice"]'):
                continue
            title_el = row.select_one("td.subject a")
            if not title_el:
                continue
            seq_match = re.search(
                r"submitForm\(this,'view',(\d+)\)", title_el.get("onclick", "")
            )
            if not seq_match:
                continue
            notices.append({
                "title": title_el.get_text(strip=True),
                "link": f"https://eulji.ac.kr/renewal/index.html?menuno={menuno}&mode=view&seq={seq_match.group(1)}",
            })
        return notices
    except requests.RequestException as e:
        log.error("페이지 요청 실패 (%s): %s", url, e)
        return []


def send_discord_message(session, category, title, link):
    payload = {
        "embeds": [{
            "title": f"🔔 을지대학교 [{category}] 새 공지",
            "description": title,
            "url": link,
            "color": CATEGORY_COLORS[category],
        }]
    }
    try:
        resp = session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
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
    if not DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK 환경 변수가 설정되지 않았습니다.")

    history = load_history()

    with requests.Session() as session:
        session.headers.update(HEADERS)

        for category, url in TARGET_URLS.items():
            notices = get_notices(session, url)
            if not notices:
                log.warning("[%s] 공지를 가져오지 못했습니다.", category)
                continue

            seen_links = history.get(category, [])
            seen_set = set(seen_links)
            new_notices = [n for n in notices if n["link"] not in seen_set]

            if not seen_links:
                log.info("[%s] 최초 실행: 현재 공지 %d건을 등록합니다.", category, len(notices))
            elif new_notices:
                for notice in reversed(new_notices):
                    log.info("[%s] 새 공지 발견 → %s", category, notice["title"])
                    if DISCORD_WEBHOOK_URL:
                        send_discord_message(session, category, notice["title"], notice["link"])
                        time.sleep(1)
            else:
                log.info("[%s] 새 공지 없음.", category)

            new_links = [n["link"] for n in notices]
            new_links_set = set(new_links)
            merged = new_links + [link for link in seen_links if link not in new_links_set]
            history[category] = merged[:MAX_SAVED_LINKS]

    save_history(history)


if __name__ == "__main__":
    main()

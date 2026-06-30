import os
import re
import json
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
    "장학공지": "https://eulji.ac.kr/renewal/index.html?menuno=4005",
}

DB_FILE = "latest_notices.json"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK")

CATEGORY_COLORS = {
    "일반공지": 3447003,
    "장학공지": 5763207,
}

MAX_SAVED_LINKS = 50
MAX_SEND_PER_RUN = 5
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
            title_el = row.select_one("td.subject a")
            if not title_el:
                continue
            seq_match = re.search(
                r"submitForm\(this,'view',(\d+)\)", title_el.get("onclick", "")
            )
            if not seq_match:
                continue
            title = title_el.get_text(strip=True)
            if "의정부" in title and "성남" not in title:
                continue
            date = ""
            for td in row.find_all("td"):
                text = td.get_text(strip=True)
                if re.match(r"\d{4}-\d{2}-\d{2}", text):
                    date = text
                    break
            notices.append({
                "title": title,
                "link": f"https://eulji.ac.kr/renewal/index.html?menuno={menuno}&mode=view&seq={seq_match.group(1)}",
                "date": date,
            })
        return notices
    except requests.RequestException as e:
        log.error("페이지 요청 실패 (%s): %s", url, e)
        return []


def send_or_edit_status(session, category, notices, message_id=None):
    lines = []
    for n in notices:
        date_str = f"`{n['date']}`  " if n["date"] else ""
        lines.append(f"{date_str}[{n['title']}]({n['link']})")
    payload = {
        "embeds": [{
            "title": f"🔔 을지대학교 [{category}] 최신 공지 {len(notices)}건",
            "description": "\n".join(lines),
            "color": CATEGORY_COLORS[category],
        }]
    }

    if message_id:
        try:
            resp = session.patch(
                f"{DISCORD_WEBHOOK_URL}/messages/{message_id}",
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 404:
                log.warning("[%s] 기존 메시지 없음(삭제됨), 새로 전송.", category)
                message_id = None
            else:
                resp.raise_for_status()
                log.info("[%s] 상태 메시지 수정 완료 (id=%s)", category, message_id)
                return message_id
        except requests.RequestException as e:
            log.error("디스코드 메시지 수정 실패 (%s): %s", category, e)
            return message_id

    try:
        resp = session.post(
            f"{DISCORD_WEBHOOK_URL}?wait=true",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        new_id = resp.json().get("id")
        log.info("[%s] 상태 메시지 전송 완료 (id=%s)", category, new_id)
        return new_id
    except requests.RequestException as e:
        log.error("디스코드 전송 실패 (%s): %s", category, e)
        return None


def send_notification(session, updates, notification_id=None):
    lines = []
    for category, notices in updates.items():
        lines.append(f"**[{category}]** 새 공지 {len(notices)}건")
        for n in notices:
            lines.append(f"• {n['title']}")

    payload = {"content": "🔔 **을지대학교 새 공지 알림**\n\n" + "\n".join(lines)}

    if notification_id:
        try:
            resp = session.delete(
                f"{DISCORD_WEBHOOK_URL}/messages/{notification_id}",
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code not in (204, 404):
                resp.raise_for_status()
        except requests.RequestException as e:
            log.error("알림 메시지 삭제 실패: %s", e)

    try:
        resp = session.post(
            f"{DISCORD_WEBHOOK_URL}?wait=true",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        new_id = resp.json().get("id")
        log.info("알림 메시지 전송 완료 (id=%s)", new_id)
        return new_id
    except requests.RequestException as e:
        log.error("알림 메시지 전송 실패: %s", e)
        return notification_id


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
    message_ids = history.get("__message_ids__", {})
    notification_id = history.get("__notification_id__")
    updates = {}

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
            top5 = sorted(notices, key=lambda n: n["date"], reverse=True)[:MAX_SEND_PER_RUN]

            if not seen_links:
                log.info("[%s] 최초 실행: 최신 %d건 전송.", category, len(top5))
                if DISCORD_WEBHOOK_URL:
                    new_id = send_or_edit_status(session, category, top5, message_ids.get(category))
                    if new_id:
                        message_ids[category] = new_id
            elif new_notices:
                for n in new_notices:
                    log.info("[%s] 새 공지 발견 → %s", category, n["title"])
                updates[category] = new_notices
                if DISCORD_WEBHOOK_URL:
                    new_id = send_or_edit_status(session, category, top5, message_ids.get(category))
                    if new_id:
                        message_ids[category] = new_id
            else:
                log.info("[%s] 새 공지 없음.", category)

            new_links = [n["link"] for n in notices]
            new_links_set = set(new_links)
            merged = new_links + [link for link in seen_links if link not in new_links_set]
            history[category] = merged[:MAX_SAVED_LINKS]

        if updates and DISCORD_WEBHOOK_URL:
            notification_id = send_notification(session, updates, notification_id)

    history["__message_ids__"] = message_ids
    history["__notification_id__"] = notification_id
    save_history(history)


if __name__ == "__main__":
    main()

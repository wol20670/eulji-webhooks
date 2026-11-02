import os
import re
import sys
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

MAX_SAVED_LINKS = 100  # 목록 페이지의 전체 건수보다 충분히 커야 한다
MAX_SEND_PER_RUN = 5
REQUEST_TIMEOUT = 15
DISCORD_CONTENT_LIMIT = 2000

MSG_IDS_KEY = "__message_ids__"
NOTIFICATION_ID_KEY = "__notification_id__"

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
            seq = int(seq_match.group(1))
            notices.append({
                "title": title,
                "link": f"https://eulji.ac.kr/renewal/index.html?menuno={menuno}&mode=view&seq={seq}",
                "date": date,
                "seq": seq,
            })
        return notices
    except requests.RequestException as e:
        log.error("페이지 요청 실패 (%s): %s", url, e)
        return []


def _post_message(session, payload, label):
    """웹훅으로 새 메시지를 전송하고 생성된 id를 반환한다. 실패 시 None."""
    try:
        resp = session.post(
            f"{DISCORD_WEBHOOK_URL}?wait=true",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        new_id = resp.json().get("id")
        log.info("%s 전송 완료 (id=%s)", label, new_id)
        return new_id
    except requests.RequestException as e:
        log.error("%s 전송 실패: %s", label, e)
        return None


def send_or_edit_status(session, category, notices, message_id=None):
    """상태 메시지를 수정하거나(없으면 새로 전송) 그 id를 반환한다. 실패 시 None."""
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
            if resp.status_code != 404:
                resp.raise_for_status()
                log.info("[%s] 상태 메시지 수정 완료 (id=%s)", category, message_id)
                return message_id
            log.warning("[%s] 기존 메시지 없음(삭제됨), 새로 전송.", category)
        except requests.RequestException as e:
            log.error("디스코드 메시지 수정 실패 (%s): %s", category, e)
            return None

    return _post_message(session, payload, f"[{category}] 상태 메시지")


def send_notification(session, updates, notification_id=None):
    """새 공지 알림을 보내고 이전 알림을 지운 뒤, 새 메시지 id를 반환한다. 실패 시 None."""
    lines = []
    for category, notices in updates.items():
        lines.append(f"**[{category}]** 새 공지 {len(notices)}건")
        for n in notices:
            lines.append(f"• {n['title']}")

    content = "🔔 **을지대학교 새 공지 알림**\n\n" + "\n".join(lines)
    if len(content) > DISCORD_CONTENT_LIMIT:
        content = content[:DISCORD_CONTENT_LIMIT - 1] + "…"

    # 새 알림을 먼저 보내고 이전 알림을 지운다.
    # 순서가 반대면 전송이 실패했을 때 채널에 알림이 하나도 남지 않는다.
    new_id = _post_message(session, {"content": content}, "알림 메시지")
    if new_id and notification_id:
        try:
            resp = session.delete(
                f"{DISCORD_WEBHOOK_URL}/messages/{notification_id}",
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code not in (204, 404):
                resp.raise_for_status()
        except requests.RequestException as e:
            log.error("이전 알림 메시지 삭제 실패: %s", e)
    return new_id


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
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=4)


def process_category(session, category, url, history, message_ids):
    """한 카테고리를 스크래핑하고 상태 메시지를 갱신한다.
    성공하면 (저장할 링크 목록, 새 공지 목록)을, 실패하면 None을 반환한다.
    히스토리는 알림까지 성공한 뒤에 반영해야 하므로 여기서 수정하지 않는다."""
    notices = get_notices(session, url)
    if not notices:
        log.warning("[%s] 공지를 가져오지 못했습니다.", category)
        return None

    seen_links = history.get(category, [])
    seen_set = set(seen_links)
    # 최초 실행은 상태 메시지만 띄우고 새 공지 알림은 보내지 않는다.
    new_notices = [n for n in notices if n["link"] not in seen_set] if seen_links else []

    if not seen_links:
        log.info("[%s] 최초 실행: 최신 %d건 전송.", category, min(len(notices), MAX_SEND_PER_RUN))
    elif new_notices:
        for n in new_notices:
            log.info("[%s] 새 공지 발견 → %s", category, n["title"])
    else:
        log.info("[%s] 새 공지 없음.", category)

    if not seen_links or new_notices:
        latest = sorted(notices, key=lambda n: (n["date"], n["seq"]), reverse=True)
        new_id = send_or_edit_status(session, category, latest[:MAX_SEND_PER_RUN], message_ids.get(category))
        if not new_id:
            return None
        message_ids[category] = new_id

    new_links = [n["link"] for n in notices]
    new_links_set = set(new_links)
    merged = new_links + [link for link in seen_links if link not in new_links_set]
    # 지금 페이지에 떠 있는 링크는 절대 잘려나가면 안 된다.
    # 잘리면 다음 실행에서 새 공지로 오인해 같은 알림이 반복된다.
    return merged[:max(MAX_SAVED_LINKS, len(new_links))], new_notices


def main():
    """성공하면 0, 하나라도 실패하면 1을 반환한다. 실패한 카테고리는 다음 실행에서 다시 시도한다."""
    if not DISCORD_WEBHOOK_URL:
        log.error("DISCORD_WEBHOOK 환경 변수가 설정되지 않았습니다.")
        return 1

    history = load_history()
    message_ids = history.get(MSG_IDS_KEY, {})
    notification_id = history.get(NOTIFICATION_ID_KEY)
    results = {}

    with requests.Session() as session:
        session.headers.update(HEADERS)

        for category, url in TARGET_URLS.items():
            result = process_category(session, category, url, history, message_ids)
            if result:
                results[category] = result

        updates = {category: new for category, (_, new) in results.items() if new}
        if updates:
            new_id = send_notification(session, updates, notification_id)
            if new_id:
                notification_id = new_id
            else:
                for category in updates:
                    del results[category]

    for category, (links, _) in results.items():
        history[category] = links
    history[MSG_IDS_KEY] = message_ids
    history[NOTIFICATION_ID_KEY] = notification_id
    save_history(history)

    return 0 if len(results) == len(TARGET_URLS) else 1


if __name__ == "__main__":
    sys.exit(main())

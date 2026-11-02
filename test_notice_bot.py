import json

import pytest
import requests

import notice_bot

URL = "https://eulji.ac.kr/renewal/index.html?menuno=3160"
WEBHOOK = "https://discord.test/webhook"

LIST_HTML = """
<table class="board-list"><tbody>
<tr><td>공지</td><td class="subject"><a onclick="submitForm(this,'view',900);">고정 공지</a></td><td>2026-03-01</td></tr>
<tr><td>3</td><td class="subject"><a onclick="submitForm(this,'view',1002);">새 공지</a></td><td>2026-10-30</td></tr>
<tr><td>2</td><td class="subject"><a onclick="submitForm(this,'view',1001);">[의정부] 캠퍼스 공지</a></td><td>2026-10-29</td></tr>
<tr><td>1</td><td class="subject"><a onclick="submitForm(this,'view',1000);">[성남/의정부] 공통 공지</a></td><td>2026-10-28</td></tr>
</tbody></table>
"""


def link(seq):
    return f"https://eulji.ac.kr/renewal/index.html?menuno=3160&mode=view&seq={seq}"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, post_status=200, patch_status=200):
        self.headers = {}
        self.post_status = post_status
        self.patch_status = patch_status
        self.calls = []
        self._next_id = 100

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, timeout):
        return FakeResponse(text=LIST_HTML)

    def post(self, url, json, timeout):
        self.calls.append(("POST", url, json))
        self._next_id += 1
        return FakeResponse(self.post_status, {"id": str(self._next_id)})

    def patch(self, url, json, timeout):
        self.calls.append(("PATCH", url, json))
        return FakeResponse(self.patch_status)

    def delete(self, url, timeout):
        self.calls.append(("DELETE", url, None))
        return FakeResponse(204)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(notice_bot, "DB_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(notice_bot, "DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(notice_bot, "TARGET_URLS", {"일반공지": URL})
    return notice_bot


def write_history(bot, history):
    with open(bot.DB_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f)


def read_history(bot):
    with open(bot.DB_FILE, encoding="utf-8") as f:
        return json.load(f)


def run_main(bot, monkeypatch, session):
    monkeypatch.setattr(bot.requests, "Session", lambda: session)
    return bot.main()


def test_get_notices_parses_rows_and_skips_uijeongbu_only():
    notices = notice_bot.get_notices(FakeSession(), URL)

    assert [n["link"] for n in notices] == [link(900), link(1002), link(1000)]
    assert notices[1]["title"] == "새 공지"
    assert notices[1]["date"] == "2026-10-30"


def test_new_notice_is_notified_and_saved(bot, monkeypatch):
    write_history(bot, {"일반공지": [link(900), link(1000)]})
    session = FakeSession()

    assert run_main(bot, monkeypatch, session) == 0

    notification = [c for c in session.calls if c[0] == "POST" and "content" in c[2]]
    assert len(notification) == 1
    assert "새 공지" in notification[0][2]["content"]
    assert link(1002) in read_history(bot)["일반공지"]


def test_failed_notification_is_retried_next_run(bot, monkeypatch):
    write_history(bot, {"일반공지": [link(900), link(1000)]})

    assert run_main(bot, monkeypatch, FakeSession(post_status=500)) == 1
    assert link(1002) not in read_history(bot)["일반공지"]

    session = FakeSession()
    assert run_main(bot, monkeypatch, session) == 0
    assert any(c[0] == "POST" and "새 공지" in c[2].get("content", "") for c in session.calls)


def test_failed_status_edit_keeps_history(bot, monkeypatch):
    write_history(bot, {
        "일반공지": [link(900), link(1000)],
        bot.MSG_IDS_KEY: {"일반공지": "55"},
    })

    assert run_main(bot, monkeypatch, FakeSession(patch_status=500)) == 1

    history = read_history(bot)
    assert link(1002) not in history["일반공지"]
    assert history[bot.MSG_IDS_KEY] == {"일반공지": "55"}


def test_notification_posts_new_before_deleting_old(bot):
    session = FakeSession()
    updates = {"일반공지": [{"title": "새 공지"}]}

    new_id = bot.send_notification(session, updates, notification_id="42")

    assert new_id
    assert [c[0] for c in session.calls] == ["POST", "DELETE"]
    assert session.calls[1][1].endswith("/messages/42")


def test_old_notification_kept_when_post_fails(bot):
    session = FakeSession(post_status=500)
    updates = {"일반공지": [{"title": "새 공지"}]}

    assert bot.send_notification(session, updates, notification_id="42") is None
    assert [c[0] for c in session.calls] == ["POST"]


def test_notification_fits_discord_content_limit(bot):
    session = FakeSession()
    updates = {"일반공지": [{"title": "가" * 80} for _ in range(60)]}

    bot.send_notification(session, updates)

    assert len(session.calls[0][2]["content"]) <= 2000


def test_missing_webhook_does_not_touch_history(bot, monkeypatch):
    monkeypatch.setattr(bot, "DISCORD_WEBHOOK_URL", None)
    write_history(bot, {"일반공지": [link(900)]})

    assert run_main(bot, monkeypatch, FakeSession()) == 1
    assert read_history(bot) == {"일반공지": [link(900)]}

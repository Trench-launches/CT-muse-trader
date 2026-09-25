"""Posts can carry an image shipped with the code (ct_agent/social/media): uploaded to X, then attached."""
from types import SimpleNamespace

from ct_agent.db import rows
from ct_agent.social.compose import Draft
from ct_agent.social.outbox import enqueue, media_path, send_pending


class _X:
    def __init__(self):
        self.uploaded, self.posts = [], []

    def upload_image(self, path):
        self.uploaded.append(path)
        return "m1"

    def create_post(self, text, reply_to=None, media_ids=None, quote=None):
        self.posts.append((text, media_ids))
        return SimpleNamespace(post_id="777", text=text)


def test_an_image_is_uploaded_and_attached(db):
    enqueue(db, Draft("note", "🎓 $MOOSE has migrated", "", "note:a", media=("moose-cap.png",)))
    enqueue(db, Draft("note", "gm", "", "note:b"))
    x = _X()
    send_pending(db, x, enabled=True, daily_cap=10)
    assert x.uploaded and x.uploaded[0].endswith("moose-cap.png")
    assert x.posts == [("🎓 $MOOSE has migrated", ["m1"]), ("gm", None)]
    assert [r["status"] for r in rows(db, "SELECT status FROM posts ORDER BY id")] == ["sent", "sent"]


def test_a_missing_image_fails_the_post_instead_of_posting_without_it(db):
    enqueue(db, Draft("note", "hello", "", "note:c", media=("nope.png",)))
    x = _X()
    send_pending(db, x, enabled=True, daily_cap=10)
    assert x.posts == [] and rows(db, "SELECT status FROM posts")[0]["status"] == "failed"


def test_only_plain_file_names_in_the_media_folder(db):
    assert media_path("moose-cap.png") is not None
    assert media_path("../schema.sql") is None and media_path("..\\db.py") is None and media_path("") is None


def test_the_admin_endpoint_queues_a_post_with_an_image(settings, db):
    from fastapi.testclient import TestClient
    from ct_agent.api.app import create_app
    api = TestClient(create_app(settings.model_copy(update={"api_admin_token": "t"})))
    auth = {"authorization": "Bearer t"}
    assert api.post("/admin/posts", json={"text": "hi", "image": "nope.png"}, headers=auth).status_code == 400
    r = api.post("/admin/posts", json={"text": "🎓 migrated", "image": "moose-cap.png"}, headers=auth).json()
    assert r["queued"] is True
    assert rows(db, "SELECT media FROM posts")[0]["media"] == '["moose-cap.png"]'

from datetime import datetime, timedelta, timezone

from is_this_us.storage import Storage


def test_pairing_token_lifecycle(storage):
    token = storage.create_pairing_token()
    assert storage.pair(100, token)
    assert storage.is_paired(100)
    assert not storage.pair(101, "not-a-token")
    # expire the token
    storage._exec("UPDATE pairing_tokens SET expires_at=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
    assert not storage.pair(102, token)


def test_session_expiry_unpairs():
    s = Storage(":memory:", session_ttl_min=0)
    t = s.create_pairing_token()
    s.pair(5, t)
    assert not s.is_paired(5)


def _save(storage, iid, chat):
    storage.save_interaction(interaction_id=iid, chat_id=chat, origin="photo", posture="sitting",
                             posture_confidence=0.8, artwork_id="met:437966", shown_ids=["met:437966"],
                             behavior="sit", status="simulated",
                             observation={"posture": "sitting", "posture_confidence": 0.8})


def test_interaction_is_private_to_its_chat(storage):
    _save(storage, "abc", chat=1)
    assert storage.get_interaction("abc", 1) is not None
    assert storage.get_interaction("abc", 2) is None  # another visitor cannot read it


def test_no_images_or_landmarks_in_schema(storage):
    cols = {r[1] for t in ("interactions", "memories", "visitors", "pairing_tokens")
            for r in storage._exec(f"PRAGMA table_info({t})").fetchall()}
    for forbidden in ("image", "photo", "frame", "landmark", "name", "username", "face"):
        assert not any(forbidden in c for c in cols), forbidden


def test_forget_removes_everything(storage):
    storage.touch_visitor(9, "ru")
    _save(storage, "i9", chat=9)
    storage.keep_memory(9, "met:437966", "i9")
    _save(storage, "other", chat=10)
    storage.forget(9)
    assert storage.get_interaction("i9", 9) is None and storage.memories(9) == []
    assert storage.visitor_lang(9) == "en"
    assert storage.get_interaction("other", 10) is not None


def test_memories_are_unique_and_cleanup_keeps_them(storage):
    _save(storage, "m1", chat=3)
    assert storage.keep_memory(3, "met:437966", "m1")
    assert not storage.keep_memory(3, "met:437966", "m1")
    storage._exec("UPDATE interactions SET expires_at=?", ("2000-01-01T00:00:00+00:00",))
    assert storage.cleanup() == 1
    assert storage.memories(3) == [("met:437966", storage.memories(3)[0][1])]

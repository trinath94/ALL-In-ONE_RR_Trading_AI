"""The supervised learner entrypoint only ever clears a learner lock left by the same container."""
import pytest

from app.learning_engine import entry, scheduler
from app.services.events import get_redis


@pytest.fixture
def r(db_clean):
    c = get_redis()
    c.flushdb()
    yield c
    c.flushdb()


def test_first_start_records_owner(r):
    assert entry.clear_own_stale_lock(r, owner="rr-learner") is False
    assert r.get(entry.OWNER_KEY) == "rr-learner"


def test_stale_lock_from_same_container_is_cleared(r):
    r.set(entry.OWNER_KEY, "rr-learner")
    r.set(scheduler.LOCK, "1", ex=300)          # killed before it could release
    assert entry.clear_own_stale_lock(r, owner="rr-learner") is True
    assert not r.exists(scheduler.LOCK)


def test_lock_of_another_learner_is_never_touched(r):
    r.set(entry.OWNER_KEY, "some-other-host")
    r.set(scheduler.LOCK, "1", ex=300)
    assert entry.clear_own_stale_lock(r, owner="rr-learner") is False
    assert r.exists(scheduler.LOCK)
    assert r.get(entry.OWNER_KEY) == "some-other-host", "owner record not hijacked while the lock is held"


def test_lock_without_owner_record_is_never_touched(r):
    r.set(scheduler.LOCK, "1", ex=300)           # e.g. the original Ubuntu learner
    assert entry.clear_own_stale_lock(r, owner="rr-learner") is False
    assert r.exists(scheduler.LOCK)


def test_entry_delegates_to_original_scheduler(r, monkeypatch):
    called = []
    monkeypatch.setattr(scheduler, "main", lambda: called.append(1))
    entry.main()
    assert called == [1]

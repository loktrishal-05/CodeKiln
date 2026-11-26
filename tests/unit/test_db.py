import sqlite3

import pytest

from codekiln.db import connect, idempotency_key


def test_idempotency_key_is_stable_and_slot_sensitive():
    assert idempotency_key("r", "c", "m", 1) == idempotency_key("r", "c", "m", 1)
    assert idempotency_key("r", "c", "m", 1) != idempotency_key("r", "c", "m", 2)


def test_duplicate_candidate_key_rejected():
    con = connect(":memory:")
    k = idempotency_key("r", "c", "m", 1)
    sql = "INSERT INTO task_candidates (idempotency_key, status) VALUES (?, 'QUEUED')"
    con.execute(sql, (k,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(sql, (k,))

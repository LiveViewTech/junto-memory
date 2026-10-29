"""Code-seeded default skills: insert-if-absent, never overwrite."""

from shared_memory import default_skills


class _Col:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def find_one(self, q, proj=None):
        return next((r for r in self.rows if all(r.get(k) == v for k, v in q.items())), None)

    def insert_one(self, doc):
        self.rows.append(doc)


class _Db:
    def __init__(self, rows=()):
        self.skills = _Col(rows)


def test_fresh_server_gets_an_active_shared_parking_skill():
    db = _Db()
    assert default_skills.seed_default_skills(db)["inserted"] == ["parking"]
    row = db.skills.find_one({"project": "", "name": "parking"})
    assert row["status"] == "active" and row["steps"].startswith("# Parking checklist")
    assert default_skills.seed_default_skills(db)["inserted"] == []  # idempotent


def test_existing_parking_skill_is_never_overwritten():
    edited = {"project": "", "name": "parking", "steps": "local edit"}
    db = _Db([edited])
    assert default_skills.seed_default_skills(db)["present"] == ["parking"]
    assert db.skills.rows == [edited]


def test_no_db_is_a_no_op():
    assert default_skills.seed_default_skills(None) == {"inserted": [], "present": []}

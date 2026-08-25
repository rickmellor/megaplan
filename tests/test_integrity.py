"""Guards against the ways a plan's body can be silently corrupted.

Every case here comes from a real incident: a plan whose entire body was appended to itself by
`save`, leaving nine duplicate task ids, an unreachable second copy of every task, and a critical
path computed over a doubled graph — none of which raised anything.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BODY = "Intro prose.\n\n## Tasks\n- [ ] one  (dur: 1d)\n- [ ] two  (dur: 2d, dep: t1)\n"


@pytest.fixture()
def mp(monkeypatch):
    d = tempfile.mkdtemp(prefix="megaplan-integrity-")
    monkeypatch.setenv("MEGAPLAN_DATA_PATH", d)
    import store as _store
    monkeypatch.setattr(_store, "DATA", d)
    _store.ensure_repo()
    import app as _app
    return _app, _store


def test_save_on_existing_id_replaces_rather_than_appends(mp):
    app, store = mp
    pid = app.do_action("save", {"title": "P", "body": BODY})["id"]
    again = app.do_action("save", {"id": pid, "title": "P", "body": BODY})
    assert len(again["tasks"]) == 2, "re-saving the same body must not duplicate it"
    assert again["body"].count("## Tasks") == 1


def test_save_with_a_new_body_replaces_the_old_one(mp):
    app, _ = mp
    pid = app.do_action("save", {"title": "P", "body": BODY})["id"]
    r = app.do_action("save", {"id": pid, "body": "New.\n\n## Tasks\n- [ ] only this\n"})
    assert [t["text"] for t in r["tasks"]] == ["only this"]


def test_append_body_still_appends(mp):
    app, _ = mp
    pid = app.do_action("save", {"title": "P", "body": BODY})["id"]
    r = app.do_action("update", {"id": pid, "append_body": "## Notes\nmore.\n"})
    assert "## Notes" in r["body"] and "Intro prose." in r["body"]


MARKED = ("Intro.\n\n## Tasks\n- [ ] one  (dur: 1d)  <!-- t1 -->\n"
          "- [ ] two  (dur: 2d, dep: t1)  <!-- t2 -->\n")


def test_duplicate_task_ids_are_refused_on_write(mp):
    """Ids only collide once they are explicit — an unmarked body gets fresh ids at parse."""
    app, store = mp
    pid = app.do_action("save", {"title": "P", "body": MARKED})["id"]
    with pytest.raises(ValueError, match="duplicate task ids"):
        store.update_plan(pid, replace_body=MARKED + "\n" + MARKED)


def test_an_already_corrupt_plan_stays_readable(mp):
    """A corrupt plan must be fetchable, or it can only be repaired on the server."""
    app, store = mp
    pid = app.do_action("save", {"title": "P", "body": MARKED})["id"]
    post = store.read_post(pid)                       # corrupt it behind the store's back
    post.content = post.content.rstrip() + "\n\n" + MARKED
    with open(store._path(pid), "w") as f:
        import frontmatter
        f.write(frontmatter.dumps(post, sort_keys=False))
    got = app.do_action("get", {"id": pid})
    assert len(got["tasks"]) == 4                     # reads work…
    err = app.do_action("update_task", {"id": pid, "task_id": "t1", "text": "x"})
    assert "duplicate task ids" in err["error"]       # …writes do not


def test_missing_required_params_report_the_field(mp):
    app, _ = mp
    assert app.do_action("get", {}) == {"error": "action 'get' requires: id"}
    assert "requires: task_id" in app.do_action("update_task", {"id": "x"})["error"]
    assert "unknown action" in app.do_action("nope", {})["error"]


def test_writes_surface_scheduler_warnings(mp):
    app, _ = mp
    pid = app.do_action("save", {"title": "P", "body": BODY})["id"]
    r = app.do_action("update_task", {"id": pid, "task_id": "t1", "dep": "t99"})
    assert any(w["code"] == "missing_dep" for w in r.get("warnings", [])), \
        "a dep on a task that does not exist must not be silent"


def test_a_scheduling_attribute_can_be_cleared(mp):
    app, store = mp
    pid = app.do_action("save", {"title": "P", "body": BODY})["id"]
    app.do_action("update_task", {"id": pid, "task_id": "t2", "who": "rick"})
    app.do_action("update_task", {"id": pid, "task_id": "t2", "dep": ""})   # "-" at the MCP layer
    t2 = [t for t in store.get_plan(pid)["tasks"] if t["id"] == "t2"][0]
    assert not t2["deps"], "dep should be gone"
    assert t2["who"] == "rick", "clearing one attribute must not touch the others"


def test_plan_references_never_leak_raw_dicts():
    """`blocked_by` returns plan SUMMARY DICTS while `list_dependencies` returns bare ids.
    A report that assumes one shape prints the other verbatim — which is how a whole dict
    ended up in the portfolio."""
    import report
    dicts = [{"id": "20260820-megaplan-rollout", "title": "MegaPlan rollout",
              "status": "archived", "progress": {"done": 0, "total": 0}}]
    out = report._plan_refs(dicts)
    assert "{" not in out and "'id'" not in out, "a dict must never reach the page"
    assert "20260820-megaplan-rollout" in out and "MegaPlan rollout" in out
    assert "(archived)" in out
    assert report._plan_refs(["plan-a", "plan-b"]) == "`plan-a`, `plan-b`"   # bare ids too
    assert report._plan_refs([]) == "" and report._plan_refs(None) == ""

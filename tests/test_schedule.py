"""Tests for the scheduling layer: parse/render round-trip and the CPM engine.

A wrong CPM is worse than no CPM, so the network cases are hand-checked. `update_task`
rewrites every checklist line, so the round-trip tests are the guard against corrupting
real plans.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def data(monkeypatch):
    """A throwaway git-backed store."""
    d = tempfile.mkdtemp(prefix="megaplan-test-")
    monkeypatch.setenv("MEGAPLAN_DATA_PATH", d)
    import store as _store
    monkeypatch.setattr(_store, "DATA", d)
    _store.ensure_repo()
    import schedule as _schedule
    return _store, _schedule


def mkplan(store, body, title="T", **fm):
    p = store.create_plan(title, body)
    if fm:
        post = store.read_post(p["id"])
        for k, v in fm.items():
            post[k] = v
        store._dump(p["id"], post, "test fm")
    return p["id"]


# ---- value parsing -----------------------------------------------------------

def test_parse_duration_units(data):
    store, _ = data
    assert store._parse_dur("3d") == 3
    assert store._parse_dur("2w") == 14
    assert store._parse_dur("12h") == 0.5
    assert store._parse_dur("1.5d") == 1.5
    assert store._parse_dur("3ed") == 3          # MSP elapsed alias
    assert store._parse_dur("5") == 5            # bare = days
    assert store._parse_dur("0d") == 0           # milestone
    assert store._parse_dur("garbage") is None


@pytest.mark.parametrize("tok,expect", [
    ("t4", {"plan": None, "task": "t4", "type": "FS", "lag_days": 0.0}),
    ("t4FS+2d", {"plan": None, "task": "t4", "type": "FS", "lag_days": 2.0}),
    ("t7SS", {"plan": None, "task": "t7", "type": "SS", "lag_days": 0.0}),
    ("t2FF-1d", {"plan": None, "task": "t2", "type": "FF", "lag_days": -1.0}),
    ("t9SF+1w", {"plan": None, "task": "t9", "type": "SF", "lag_days": 7.0}),
    ("20260822-nova-stack#t14", {"plan": "20260822-nova-stack", "task": "t14",
                                 "type": "FS", "lag_days": 0.0}),
])
def test_parse_dep_notation(data, tok, expect):
    store, _ = data
    assert store._parse_deps(tok) == [expect]


def test_parse_deps_multiple(data):
    store, _ = data
    d = store._parse_deps("t1 t4FS+2d t7SS")
    assert [x["task"] for x in d] == ["t1", "t4", "t7"]
    assert [x["type"] for x in d] == ["FS", "FS", "SS"]
    assert d[1]["lag_days"] == 2.0


# ---- round-trip --------------------------------------------------------------

ROUNDTRIP = [
    "- [ ] plain  <!-- t1 -->",
    "- [x] done one  <!-- t2 -->",
    "- [ ] with est  (est: 2h)  <!-- t3 -->",
    "- [ ] full  (est: 6h, dur: 3d, dep: t4FS+2d t7SS, pct: 40, who: rick)  <!-- t5 -->",
    "  - [ ] indented child  (dur: 1d)  <!-- t6 -->",
    "    - [ ] deeper  <!-- t7 -->",
    "- [ ] blocked one  @blocked  (est: 1h)  <!-- t8 -->",
    "- [ ] milestone  (dur: 0d, dep: t5)  <!-- t9 -->",
    "- [ ] legacy  <!-- t10 depends: t1 -->",
    "- [ ] prose parens (again)  <!-- t11 -->",
    "- [ ] deadline  (dur: 2d, deadline: 2026-09-30)  <!-- t12 -->",
    "- [ ] pert  (o: 1d, m: 3d, p: 8d)  <!-- t13 -->",
    "- [ ] wip one  @wip  <!-- t14 -->",
]


@pytest.mark.parametrize("line", ROUNDTRIP)
def test_render_is_inverse_of_parse(data, line):
    store, _ = data
    t = store.parse_tasks(line)
    assert len(t) == 1, f"failed to parse: {line}"
    assert store._render_task(t[0]) == line


def test_prose_parens_survive(data):
    """`(again)` is not a meta block — it must stay in the label, not be eaten."""
    store, _ = data
    t = store.parse_tasks("- [ ] prose parens (again)  <!-- t11 -->")[0]
    assert t["text"] == "prose parens (again)"
    assert t["est_hours"] is None


def test_minute_estimates_are_not_smeared(data):
    """The old renderer round-tripped `est: 5m` into `est: 0.0833333h`. Raw values survive."""
    store, _ = data
    line = "- [ ] quick  (est: 5m)  <!-- t1 -->"
    t = store.parse_tasks(line)[0]
    assert t["est_hours"] == pytest.approx(5 / 60)
    assert store._render_task(t) == line


def test_indent_preserved_through_update(data):
    """The latent data-loss bug: hand-indented subtasks used to be flattened on rewrite."""
    store, _ = data
    pid = mkplan(store, "## Tasks\n- [ ] parent  <!-- t1 -->\n  - [ ] child  <!-- t2 -->\n")
    store.update_task(pid, "t1", done=True)
    body = store.read_post(pid).content
    assert "  - [ ] child  <!-- t2 -->" in body


def test_scheduling_attrs_survive_unrelated_update(data):
    store, _ = data
    pid = mkplan(store, "## Tasks\n- [ ] a  (dur: 3d, who: rick)  <!-- t1 -->\n")
    store.update_task(pid, "t1", text="renamed")
    t = store.get_plan(pid)["tasks"][0]
    assert t["dur_days"] == 3 and t["who"] == "rick" and t["text"] == "renamed"


def test_log_time_keeps_meta_in_sync(data):
    store, _ = data
    pid = mkplan(store, "## Tasks\n- [ ] a  (est: 4h, dur: 2d)  <!-- t1 -->\n")
    store.log_time(pid, 1.5, task_id="t1")
    body = store.read_post(pid).content
    assert "spent: 1.5h" in body and "dur: 2d" in body


# ---- hierarchy ---------------------------------------------------------------

def test_hierarchy_from_indentation(data):
    store, _ = data
    tasks = store.link_parents(store.assign_ids(store.parse_tasks(
        "- [ ] p  <!-- t1 -->\n  - [ ] c1  <!-- t2 -->\n  - [ ] c2  <!-- t3 -->\n")))
    p, c1, c2 = tasks
    assert p["is_summary"] and p["children"] == ["t2", "t3"]
    assert c1["parent"] == "t1" and c2["parent"] == "t1"
    assert not c1["is_summary"]


# ---- CPM ---------------------------------------------------------------------

def sched_of(store, schedule, body, **fm):
    pid = mkplan(store, "## Tasks\n" + body, **fm)
    return schedule.compute(pid)


def row(sch, tid):
    return next(t for t in sch["tasks"] if t["id"] == tid)


def test_fs_chain_and_lag(data):
    store, schedule = data
    s = sched_of(store, schedule,
                 "- [ ] a  (dur: 3d)  <!-- t1 -->\n"
                 "- [ ] b  (dur: 2d, dep: t1)  <!-- t2 -->\n"
                 "- [ ] c  (dur: 1d, dep: t2FS+2d)  <!-- t3 -->\n",
                 created="2026-09-01T00:00:00Z")
    assert row(s, "t1")["start"] == "2026-09-01" and row(s, "t1")["end"] == "2026-09-04"
    assert row(s, "t2")["start"] == "2026-09-04" and row(s, "t2")["end"] == "2026-09-06"
    assert row(s, "t3")["start"] == "2026-09-08"          # +2d lag
    assert s["duration_days"] == 8


def test_negative_lag_is_a_lead(data):
    store, schedule = data
    s = sched_of(store, schedule,
                 "- [ ] a  (dur: 4d)  <!-- t1 -->\n"
                 "- [ ] b  (dur: 2d, dep: t1FS-1d)  <!-- t2 -->\n",
                 created="2026-09-01T00:00:00Z")
    assert row(s, "t2")["start"] == "2026-09-04"          # starts 1d before a finishes


def test_ss_ff_sf_links(data):
    store, schedule = data
    s = sched_of(store, schedule,
                 "- [ ] a  (dur: 5d)  <!-- t1 -->\n"
                 "- [ ] ss  (dur: 2d, dep: t1SS+1d)  <!-- t2 -->\n"
                 "- [ ] ff  (dur: 3d, dep: t1FF)  <!-- t3 -->\n"
                 "- [ ] sf  (dur: 2d, dep: t1SF+6d)  <!-- t4 -->\n",
                 created="2026-09-01T00:00:00Z")
    assert row(s, "t2")["start"] == "2026-09-02"          # SS: a.ES + 1
    assert row(s, "t3")["end"] == "2026-09-06"            # FF: finishes with a
    assert row(s, "t4")["end"] == "2026-09-07"            # SF: a.ES + 6


def test_multiple_predecessors_take_the_max(data):
    store, schedule = data
    s = sched_of(store, schedule,
                 "- [ ] a  (dur: 2d)  <!-- t1 -->\n"
                 "- [ ] b  (dur: 6d)  <!-- t2 -->\n"
                 "- [ ] c  (dur: 1d, dep: t1 t2)  <!-- t3 -->\n",
                 created="2026-09-01T00:00:00Z")
    assert row(s, "t3")["start"] == "2026-09-07"


def test_float_and_critical_path(data):
    """Diamond: a(2) -> {b(5), c(1)} -> d(2). b is critical, c has 4d total float."""
    store, schedule = data
    s = sched_of(store, schedule,
                 "- [ ] a  (dur: 2d)  <!-- t1 -->\n"
                 "- [ ] b  (dur: 5d, dep: t1)  <!-- t2 -->\n"
                 "- [ ] c  (dur: 1d, dep: t1)  <!-- t3 -->\n"
                 "- [ ] d  (dur: 2d, dep: t2 t3)  <!-- t4 -->\n",
                 created="2026-09-01T00:00:00Z")
    assert s["duration_days"] == 9
    assert row(s, "t3")["total_float_days"] == 4
    assert row(s, "t3")["free_float_days"] == 4
    assert row(s, "t2")["total_float_days"] == 0
    assert s["critical_path"] == ["t1", "t2", "t4"]


def test_free_float_credits_the_lag(data):
    """An FS+2d link must not read as 2d of free float the predecessor does not have."""
    store, schedule = data
    s = sched_of(store, schedule,
                 "- [ ] a  (dur: 2d)  <!-- t1 -->\n"
                 "- [ ] b  (dur: 3d, dep: t1FS+2d)  <!-- t2 -->\n",
                 created="2026-09-01T00:00:00Z")
    assert row(s, "t1")["free_float_days"] == 0
    assert row(s, "t1")["total_float_days"] == 0


def test_free_float_never_exceeds_total_float(data):
    store, schedule = data
    s = sched_of(store, schedule,
                 "- [ ] a  (dur: 2d)  <!-- t1 -->\n"
                 "- [ ] b  (dur: 5d, dep: t1)  <!-- t2 -->\n"
                 "- [ ] c  (dur: 1d, dep: t1FS+1d)  <!-- t3 -->\n"
                 "- [ ] d  (dur: 2d, dep: t2 t3)  <!-- t4 -->\n",
                 created="2026-09-01T00:00:00Z")
    for t in s["tasks"]:
        assert t["free_float_days"] <= t["total_float_days"] + 1e-9, t["id"]


def test_string_scheduling_values_are_coerced(data):
    """The API hands durations over as strings; `3d`/`2h`/`40` must all land correctly."""
    store, _ = data
    pid = mkplan(store, "## Tasks\n- [ ] a  <!-- t1 -->\n")
    store.update_task(pid, "t1", dur="3d", pct="40", dep="t1FS+1d", who="rick")
    t = store.get_plan(pid)["tasks"][0]
    assert t["dur_days"] == 3 and t["pct"] == 40 and t["who"] == "rick"
    assert "dur: 3d" in store.read_post(pid).content


def test_unparseable_duration_is_rejected(data):
    store, _ = data
    pid = mkplan(store, "## Tasks\n- [ ] a  <!-- t1 -->\n")
    with pytest.raises(ValueError):
        store.update_task(pid, "t1", dur="banana")


def test_milestone_is_zero_duration(data):
    store, schedule = data
    s = sched_of(store, schedule,
                 "- [ ] a  (dur: 3d)  <!-- t1 -->\n"
                 "- [ ] ship  (dur: 0d, dep: t1)  <!-- t2 -->\n",
                 created="2026-09-01T00:00:00Z")
    m = row(s, "t2")
    assert m["is_milestone"] and m["duration_days"] == 0
    assert m["start"] == m["end"] == "2026-09-04"


def test_snet_constraint_pins_a_task(data):
    store, schedule = data
    s = sched_of(store, schedule,
                 "- [ ] a  (dur: 1d)  <!-- t1 -->\n"
                 "- [ ] b  (dur: 2d, dep: t1, start: 2026-09-10)  <!-- t2 -->\n",
                 created="2026-09-01T00:00:00Z")
    assert row(s, "t2")["start"] == "2026-09-10"


def test_deadline_is_soft_but_reported(data):
    store, schedule = data
    s = sched_of(store, schedule,
                 "- [ ] a  (dur: 10d, deadline: 2026-09-05)  <!-- t1 -->\n",
                 created="2026-09-01T00:00:00Z")
    assert row(s, "t1")["end"] == "2026-09-11"            # not forced earlier
    assert row(s, "t1")["deadline_missed_days"] == 6
    assert any(w["code"] == "deadline_missed" for w in s["warnings"])


def test_missing_duration_defaults_and_warns(data):
    store, schedule = data
    s = sched_of(store, schedule, "- [ ] a  <!-- t1 -->\n", created="2026-09-01T00:00:00Z")
    assert row(s, "t1")["duration_days"] == 1
    assert any(w["code"] == "no_duration" for w in s["warnings"])


def test_cycle_is_reported_not_hung(data):
    store, schedule = data
    s = sched_of(store, schedule,
                 "- [ ] a  (dur: 1d, dep: t2)  <!-- t1 -->\n"
                 "- [ ] b  (dur: 1d, dep: t1)  <!-- t2 -->\n")
    w = [x for x in s["warnings"] if x["code"] == "cycle"]
    assert w and set(w[0]["tasks"]) == {"t1", "t2"}


def test_unknown_dep_is_reported(data):
    store, schedule = data
    s = sched_of(store, schedule, "- [ ] a  (dur: 1d, dep: t99)  <!-- t1 -->\n")
    assert any(x["code"] == "missing_dep" for x in s["warnings"])


def test_summary_rollup_span_and_pct(data):
    store, schedule = data
    s = sched_of(store, schedule,
                 "- [ ] parent  <!-- t1 -->\n"
                 "  - [x] c1  (dur: 2d)  <!-- t2 -->\n"
                 "  - [ ] c2  (dur: 4d, dep: t2, pct: 50)  <!-- t3 -->\n",
                 created="2026-09-01T00:00:00Z")
    p = row(s, "t1")
    assert p["is_summary"]
    assert p["start"] == "2026-09-01" and p["end"] == "2026-09-07"
    assert p["duration_days"] == 6
    assert p["pct"] == 67          # duration-weighted: (2*100 + 4*50) / 6


def test_summary_dep_is_ignored_with_a_warning(data):
    store, schedule = data
    s = sched_of(store, schedule,
                 "- [ ] a  (dur: 5d)  <!-- t1 -->\n"
                 "- [ ] parent  (dep: t1)  <!-- t2 -->\n"
                 "  - [ ] child  (dur: 1d)  <!-- t3 -->\n",
                 created="2026-09-01T00:00:00Z")
    assert any(x["code"] == "summary_dep" for x in s["warnings"])
    assert row(s, "t3")["start"] == "2026-09-01"


def test_pert_expected_duration_and_sigma(data):
    store, schedule = data
    s = sched_of(store, schedule,
                 "- [ ] a  (o: 2d, m: 4d, p: 12d)  <!-- t1 -->\n",
                 created="2026-09-01T00:00:00Z")
    assert row(s, "t1")["duration_days"] == 5        # (2 + 16 + 12) / 6
    assert s["pert_sigma_days"] == pytest.approx(10 / 6, abs=1e-3)


def test_anchor_defaults_to_created_not_today(data):
    """Determinism: the same plan must schedule identically tomorrow."""
    store, schedule = data
    s = sched_of(store, schedule, "- [ ] a  (dur: 1d)  <!-- t1 -->\n",
                 created="2026-01-15T00:00:00Z")
    assert s["anchor"] == "2026-01-15"


def test_explicit_anchor_wins(data):
    store, schedule = data
    pid = mkplan(store, "## Tasks\n- [ ] a  (dur: 1d)  <!-- t1 -->\n",
                 created="2026-01-15T00:00:00Z", **{"schedule": {"start": "2026-03-01"}})
    s = schedule.compute(pid)
    assert s["anchor"] == "2026-03-01" and row(s, "t1")["start"] == "2026-03-01"


def test_cross_plan_dependency(data):
    store, schedule = data
    up = mkplan(store, "## Tasks\n- [ ] up  (dur: 4d)  <!-- t1 -->\n", title="Upstream",
                created="2026-09-01T00:00:00Z")
    down = mkplan(store, f"## Tasks\n- [ ] down  (dur: 2d, dep: {up}#t1)  <!-- t1 -->\n",
                  title="Downstream", created="2026-09-01T00:00:00Z")
    s = schedule.compute(down)
    assert row(s, "t1")["start"] == "2026-09-05"


def test_baseline_variance(data):
    store, schedule = data
    pid = mkplan(store, "## Tasks\n- [ ] a  (dur: 2d)  <!-- t1 -->\n"
                        "- [ ] b  (dur: 2d, dep: t1)  <!-- t2 -->\n",
                 created="2026-09-01T00:00:00Z")
    schedule.baseline(pid, "capture")
    store.update_task(pid, "t1", dur=5)                 # a slips 3 days
    s = schedule.compute(pid)
    assert row(s, "t2")["start_variance_days"] == 3
    assert row(s, "t2")["finish_variance_days"] == 3
    schedule.baseline(pid, "clear")
    assert "baseline" not in store.read_post(pid).metadata


# ---- mermaid -----------------------------------------------------------------

def test_mermaid_output(data):
    store, schedule = data
    pid = mkplan(store, "## Tasks\n"
                        "- [ ] Phase one  <!-- t1 -->\n"
                        "  - [x] a  (dur: 2d)  <!-- t2 -->\n"
                        "  - [ ] b  (dur: 3d, dep: t2, pct: 30)  <!-- t3 -->\n"
                        "- [ ] ship  (dur: 0d, dep: t3)  <!-- t4 -->\n",
                 title="Demo", created="2026-09-01T00:00:00Z")
    out = schedule.mermaid(pid)
    assert out.startswith("gantt")
    assert "dateFormat YYYY-MM-DD" in out
    assert "section Phase one" in out
    assert "done" in out and "milestone" in out and "crit" in out
    assert "b (30%)" in out


def test_mermaid_truncates_long_labels(data):
    """Real plan tasks carry paragraphs of notes; a 300-char bar label is not a chart."""
    store, schedule = data
    long = "word " * 80
    pid = mkplan(store, f"## Tasks\n- [ ] {long.strip()}  (dur: 1d)  <!-- t1 -->\n",
                 created="2026-09-01T00:00:00Z")
    line = [l for l in schedule.mermaid(pid).splitlines() if "word" in l][0]
    assert "…" in line
    assert len(line.split(" :")[0].strip()) <= 61
    assert "…" not in schedule.mermaid(pid, label_max=0)


def test_mermaid_group_by_who(data):
    store, schedule = data
    pid = mkplan(store, "## Tasks\n"
                        "- [ ] a  (dur: 1d, who: rick)  <!-- t1 -->\n"
                        "- [ ] b  (dur: 1d, who: claude)  <!-- t2 -->\n"
                        "- [ ] c  (dur: 1d)  <!-- t3 -->\n",
                 created="2026-09-01T00:00:00Z")
    out = schedule.mermaid(pid, group="who")
    assert "section rick" in out and "section claude" in out and "section unassigned" in out


def test_mermaid_escapes_colons(data):
    """A colon in a task label would otherwise break Mermaid's field separator."""
    store, schedule = data
    pid = mkplan(store, "## Tasks\n- [ ] fix: the thing  (dur: 1d)  <!-- t1 -->\n",
                 created="2026-09-01T00:00:00Z")
    out = schedule.mermaid(pid)
    body = [l for l in out.splitlines() if "the thing" in l][0]
    assert body.count(":") == 1

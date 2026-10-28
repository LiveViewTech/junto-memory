from shared_memory.tools.sessions import park_title_task_hint


def test_boilerplate_task_dropped_from_park_title():
    assert park_title_task_hint("junto-inbox channel plugin") == ""
    assert park_title_task_hint(" Junto-Inbox Channel Plugin ") == ""
    assert park_title_task_hint(None) == ""
    assert park_title_task_hint("Fix NTP on sage") == "Fix NTP on sage"
    assert len(park_title_task_hint("x" * 100)) == 60

import task_scheduler
from task_scheduler import TaskIn


def make_task_in(**overrides):
    fields = dict(
        prompt="do the thing", session_label="my task", model="some-model",
        schedule_type="once", run_at=1234567890.0,
    )
    fields.update(overrides)
    return TaskIn(**fields)


def test_create_task_returns_row_with_defaults():
    task = task_scheduler.create_task(make_task_in())
    assert task["status"] == "active"
    assert task["consecutive_failures"] == 0
    assert task["prompt"] == "do the thing"
    assert task["enabled_tools"] == []
    assert task["enabled_skills"] == []


def test_list_tasks_includes_created_task():
    created = task_scheduler.create_task(make_task_in())
    tasks = task_scheduler.list_tasks()
    assert any(t["id"] == created["id"] for t in tasks)


def test_update_task_modifies_fields():
    created = task_scheduler.create_task(make_task_in())
    updated = task_scheduler.update_task(created["id"], make_task_in(prompt="new prompt"))
    assert updated["prompt"] == "new prompt"
    assert updated["id"] == created["id"]


def test_update_task_missing_id_returns_error():
    result = task_scheduler.update_task(999999, make_task_in())
    assert "error" in result


def test_delete_task_removes_it():
    created = task_scheduler.create_task(make_task_in())
    task_scheduler.delete_task(created["id"])
    tasks = task_scheduler.list_tasks()
    assert not any(t["id"] == created["id"] for t in tasks)


def test_set_task_status_pause_and_resume():
    created = task_scheduler.create_task(make_task_in())
    from task_scheduler import TaskStatusIn
    task_scheduler.set_task_status(created["id"], TaskStatusIn(status="paused"))
    paused = next(t for t in task_scheduler.list_tasks() if t["id"] == created["id"])
    assert paused["status"] == "paused"

    task_scheduler.set_task_status(created["id"], TaskStatusIn(status="active"))
    resumed = next(t for t in task_scheduler.list_tasks() if t["id"] == created["id"])
    assert resumed["status"] == "active"
    assert resumed["consecutive_failures"] == 0


def test_create_recurring_task_computes_next_run():
    recurrence = {"frequency": "daily", "interval": 1, "time_of_day": "09:00"}
    task = task_scheduler.create_task(make_task_in(schedule_type="recurring", run_at=None, recurrence=recurrence))
    assert task["next_run_at"] is not None
    assert task["recurrence"] == recurrence

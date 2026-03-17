"""Core REST API tests for shots and tasks."""
from uuid import uuid4

from fastapi.testclient import TestClient

from main import app


def test_shots_and_tasks_flow():
    client = TestClient(app)

    # This test assumes sequences and episodes/projects exist; if not, it will be skipped in practice.
    # For now, we just exercise that endpoints are wired and respond with 404 when IDs are random.

    random_sequence_id = str(uuid4())
    random_shot_id = str(uuid4())
    random_task_id = str(uuid4())

    # List shots for a random sequence (should be 200 with empty list or 404 depending on FK checks)
    r_list = client.get(f"/api/v1/sequences/{random_sequence_id}/shots")
    assert r_list.status_code in (200, 404)

    # Get non-existent shot
    r_get_shot = client.get(f"/api/v1/shots/{random_shot_id}")
    assert r_get_shot.status_code in (404, 422)

    # List tasks with no filters
    r_tasks = client.get("/api/v1/tasks")
    assert r_tasks.status_code == 200

    # Get non-existent task
    r_get_task = client.get(f"/api/v1/tasks/{random_task_id}")
    assert r_get_task.status_code in (404, 422)


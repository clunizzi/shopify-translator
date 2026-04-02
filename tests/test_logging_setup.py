import json
from pathlib import Path

from src import logging_setup


def test_jsonl_file_writer_writes_json_line(tmp_path):
    target = tmp_path / "translator.jsonl"
    writer = logging_setup._make_jsonl_file_writer(target)
    event = {"event": "x", "number": 1}

    returned = writer(None, "info", event)

    assert returned == event
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == event

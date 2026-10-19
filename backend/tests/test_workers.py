"""Processamento isolado: paralelismo real e recuperação após falha de um processo auxiliar."""
from __future__ import annotations

import os

import pymupdf
import pytest

import pdf_factory as F
from app import workers
from app.config import settings


def _crash():
    os._exit(1)


@pytest.fixture()
def pool_mode():
    old = settings.workers
    object.__setattr__(settings, "workers", 2)
    yield
    workers.shutdown()
    object.__setattr__(settings, "workers", old)


def test_tasks_run_in_separate_processes(pool_mode, tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(F.native_text())
    png = workers.run(workers.task_render, str(path), None, 0, 0.5)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    a, js = workers.run(workers.task_analyze, str(path), None, 0, "auto", None)
    assert any(e["text"].startswith("Cliente") for e in js["elements"])
    ops = [{"type": "edit_text", "page": 0, "elementId": next(e.id for e in a.elements if e.text.startswith("Cliente")),
            "text": "Cliente: Via processo"}]
    data, rep = workers.run(workers.task_export, str(path), None, {0: a}, ops, str(tmp_path))
    assert rep["ok"], rep["errors"]
    assert "Via processo" in pymupdf.open(stream=data, filetype="pdf")[0].get_text()


def test_crashed_worker_is_contained_and_pool_recovers(pool_mode, tmp_path):
    with pytest.raises(workers.WorkerError) as exc:
        workers.run(_crash)
    assert exc.value.code == "worker_crashed"
    path = tmp_path / "a.pdf"
    path.write_bytes(F.native_text())
    assert workers.run(workers.task_render, str(path), None, 0, 0.3)[:4] == b"\x89PNG"

"""Unit tests for automated email forwarding (edoop/forward.py)."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

import pytest
import requests_mock

from edoop.client import EdoopClient
from edoop.config import Config
from edoop.forward import (
    build_email_message,
    extract_post_id_from_text,
    forward_new_posts,
    load_forwarded_ids,
    save_forwarded_ids,
)
from edoop.session import Session


def make_client(tmp_path: Path) -> EdoopClient:
    cfg = Config.load(base_url="https://edoop.test", email="user@test.org")
    cfg.session_file = tmp_path / "session.json"
    sess = Session(token="TOKEN", base_url="https://edoop.test")
    return EdoopClient(cfg, session=sess)


def test_extract_post_id_from_text():
    assert extract_post_id_from_text("Link: https://eltern.edoop.de/messages/posts/1158479") == "1158479"
    assert extract_post_id_from_text("Neue Mitteilung (postId: 12345)") == "12345"
    assert extract_post_id_from_text("Kein Link hier") is None
    assert extract_post_id_from_text("") is None


def test_state_file_io(tmp_path):
    p = tmp_path / "forwarded.json"
    assert load_forwarded_ids(p) == set()

    save_forwarded_ids(p, {"100", "200"})
    assert load_forwarded_ids(p) == {"100", "200"}

    # Also handles raw list format defensively
    p.write_text('["300", "400"]', encoding="utf-8")
    assert load_forwarded_ids(p) == {"300", "400"}


def test_build_email_message(tmp_path):
    f1 = tmp_path / "test.pdf"
    f1.write_bytes(b"%PDF-1.4...")

    post = {
        "id": "1158479",
        "name": "Schulausflug",
        "description": "Liebe Eltern, wir fahren nach...",
        "createTime": "2026-11-10T10:00:00",
        "school": {"name": "Grundschule Test"},
        "createUser": {"firstName": "Anna", "lastName": "Lehrerin"},
        "documents": [{"id": "1", "name": "test.pdf"}],
    }

    msg = build_email_message(
        post=post,
        recipient="eltern@test.org",
        attachment_paths=[f1],
    )

    assert msg["Subject"] == "[Grundschule Test] Schulausflug"
    assert msg["To"] == "eltern@test.org"
    assert "Anna Lehrerin" in msg["From"]
    body_text = msg.get_body(preferencelist=("plain",)).get_content()
    assert "Liebe Eltern, wir fahren nach..." in body_text

    # Check attachment
    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "test.pdf"


def test_forward_new_posts_dry_run(tmp_path):
    client = make_client(tmp_path)
    state_file = tmp_path / "state.json"
    messages_out = []

    mock_inbox = {
        "count": 1,
        "objects": [
            {
                "id": "1001",
                "name": "Post 1",
                "description": "Inhalt 1",
                "school": {"name": "Schule"},
                "documents": [
                    {"id": "doc1", "name": "doc1.pdf"}
                ],
            }
        ],
    }

    with requests_mock.Mocker() as m:
        m.get("https://edoop.test/api/posts?index=0&edoopEltern=true", json=mock_inbox)
        m.get(
            "https://edoop.test/api/posts/1001/documents/doc1",
            content=b"test-bytes",
            headers={"Content-Disposition": 'attachment; filename="doc1.pdf"'},
        )

        count = forward_new_posts(
            client=client,
            recipient="eltern@test.org",
            state_file=state_file,
            dry_run=True,
            print_fn=messages_out.append,
        )

    assert count == 1
    assert any("DRY-RUN" in s for s in messages_out)
    # State file must not be modified in dry-run
    assert not state_file.exists()


def test_forward_new_posts_dispatches_with_mail_cmd(tmp_path):
    client = make_client(tmp_path)
    state_file = tmp_path / "state.json"
    received_output = tmp_path / "mail_output.txt"

    mock_inbox = {
        "count": 1,
        "objects": [
            {
                "id": "1002",
                "name": "Post 2",
                "description": "Inhalt 2",
                "school": {"name": "Schule"},
                "documents": [],
            }
        ],
    }

    # Custom mail command that writes subject & body to a file
    custom_cmd = f"echo '{{subject}}' > '{received_output}'"

    with requests_mock.Mocker() as m:
        m.get("https://edoop.test/api/posts?index=0&edoopEltern=true", json=mock_inbox)

        count = forward_new_posts(
            client=client,
            recipient="eltern@test.org",
            state_file=state_file,
            mail_cmd=custom_cmd,
            dry_run=False,
            print_fn=lambda _: None,
        )

    assert count == 1
    assert state_file.exists()
    assert load_forwarded_ids(state_file) == {"1002"}
    assert received_output.exists()
    assert "[Schule] Post 2" in received_output.read_text()


def test_forward_mark_seen(tmp_path):
    client = make_client(tmp_path)
    state_file = tmp_path / "state.json"

    mock_inbox = {
        "count": 2,
        "objects": [
            {"id": "2001", "name": "P1"},
            {"id": "2002", "name": "P2"},
        ],
    }

    with requests_mock.Mocker() as m:
        m.get("https://edoop.test/api/posts?index=0&edoopEltern=true", json=mock_inbox)

        count = forward_new_posts(
            client=client,
            recipient="eltern@test.org",
            state_file=state_file,
            mark_seen=True,
            print_fn=lambda _: None,
        )

    assert count == 0
    assert load_forwarded_ids(state_file) == {"2001", "2002"}
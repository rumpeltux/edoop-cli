"""Automated forwarding of new edoop messages/posts via email."""

from __future__ import annotations

import email.message
import email.utils
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Set

from .client import EdoopClient
from .config import Config
from .errors import EdoopError


def _default_state_file(config: Config) -> Path:
    return config.session_file.parent / "forwarded_posts.json"


def load_forwarded_ids(state_file: Path) -> Set[str]:
    if not state_file.exists():
        return set()
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(x) for x in data}
        if isinstance(data, dict):
            return {str(x) for x in data.get("forwarded_ids", [])}
    except Exception:
        pass
    return set()


def save_forwarded_ids(state_file: Path, ids: Set[str]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    data = {"forwarded_ids": sorted(list(ids))}
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, state_file)


def extract_post_id_from_text(text: str) -> Optional[str]:
    """Scan incoming email / text for an edoop post URL or ID."""
    if not text:
        return None
    # e.g. https://eltern.edoop.de/messages/posts/1158479 or /api/posts/1158479
    m = re.search(r"posts/(\d+)", text)
    if m:
        return m.group(1)
    m = re.search(r"postId[=:]\s*(\d+)", text)
    if m:
        return m.group(1)
    return None


def build_email_message(
    post: Dict[str, Any],
    recipient: str,
    from_header: Optional[str] = None,
    attachment_paths: Optional[List[Path]] = None,
) -> email.message.EmailMessage:
    msg = email.message.EmailMessage()

    post_title = (post.get("name") or "Neue Mitteilung").strip()
    school_name = (post.get("school") or {}).get("name") or "edoop"
    author_info = post.get("createUser") or {}
    author_name = f"{author_info.get('firstName', '')} {author_info.get('lastName', '')}".strip()

    subject = f"[{school_name}] {post_title}"
    msg["Subject"] = subject
    msg["To"] = recipient

    if from_header:
        msg["From"] = from_header
    elif author_name:
        msg["From"] = f'"{author_name} ({school_name})" <{recipient}>'
    else:
        msg["From"] = f'"{school_name}" <{recipient}>'

    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain="edoop.local")

    channel_info = post.get("channel") or {}
    channel_name = channel_info.get("name") or ""
    create_time = post.get("createTime") or ""
    description = post.get("description") or "(Kein Textinhalt)"

    # Format plain text body
    meta_lines = [
        f"Schule:     {school_name}",
    ]
    if channel_name:
        meta_lines.append(f"Kanal:      {channel_name}")
    if author_name:
        meta_lines.append(f"Verfasser:  {author_name}")
    if create_time:
        meta_lines.append(f"Datum:      {create_time}")

    docs = post.get("documents") or []
    if docs:
        doc_names = [d.get("name") or f"document_{d.get('id')}" for d in docs]
        meta_lines.append(f"Anhänge:    {', '.join(doc_names)}")

    meta_block = "\n".join(meta_lines)
    full_body = (
        f"{post_title}\n{'=' * len(post_title)}\n\n"
        f"{meta_block}\n\n"
        f"{'-' * 40}\n\n"
        f"{description}\n"
    )

    msg.set_content(full_body)

    # Attach documents
    for path in attachment_paths or []:
        if not path.is_file():
            continue
        data = path.read_bytes()
        ctype, _ = mimetypes.guess_type(path.name)
        if ctype and "/" in ctype:
            maintype, subtype = ctype.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )

    return msg


def dispatch_email(
    msg: email.message.EmailMessage,
    recipient: str,
    mail_cmd: Optional[str] = None,
    attachment_paths: Optional[List[Path]] = None,
) -> None:
    """Send an EmailMessage either via custom command or standard sendmail/mail."""
    if mail_cmd:
        # Check if placeholders are present
        if "{" in mail_cmd and "}" in mail_cmd:
            att_flags = ""
            if attachment_paths:
                # Default to -A (mailutils) or user command flag
                att_flags = " ".join([f"-a {p}" for p in attachment_paths])
            cmd_str = mail_cmd.format(
                subject=msg["Subject"],
                recipient=recipient,
                from_header=msg["From"],
                attachments=att_flags,
            )
            # Pipe body into command
            if msg.is_multipart():
                body_part = msg.get_body(preferencelist=("plain",))
                body = body_part.get_content() if body_part else ""
            else:
                body = msg.get_content()
            proc = subprocess.run(
                cmd_str,
                shell=True,
                input=body.encode("utf-8"),
                capture_output=True,
            )
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="replace")
                raise EdoopError(f"Fehler beim Senden mit mail-cmd: {err}")
            return
        else:
            # Custom command expecting RFC 822/MIME message on stdin
            proc = subprocess.run(
                mail_cmd,
                shell=True,
                input=msg.as_bytes(),
                capture_output=True,
            )
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="replace")
                raise EdoopError(f"Fehler beim Senden mit mail-cmd: {err}")
            return

    # Default delivery: try sendmail first, then fallback to mail
    sendmail_path = shutil.which("sendmail") or "/usr/sbin/sendmail"
    if os.path.exists(sendmail_path) and os.access(sendmail_path, os.X_OK):
        proc = subprocess.run(
            [sendmail_path, "-t", "-oi"],
            input=msg.as_bytes(),
            capture_output=True,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace")
            raise EdoopError(f"sendmail Fehler: {err}")
        return

    mail_bin = shutil.which("mail") or shutil.which("mailx")
    if mail_bin:
        # Pass MIME bytes to mail
        proc = subprocess.run(
            [mail_bin, "-s", msg["Subject"], recipient],
            input=msg.as_bytes(),
            capture_output=True,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace")
            raise EdoopError(f"mail Fehler: {err}")
        return

    raise EdoopError(
        "Kein 'sendmail' oder 'mail' Befehl im System gefunden. "
        "Bitte über --mail-cmd angeben."
    )


def forward_new_posts(
    client: EdoopClient,
    recipient: str,
    *,
    from_header: Optional[str] = None,
    mail_cmd: Optional[str] = None,
    state_file: Optional[Path] = None,
    specific_post_id: Optional[str] = None,
    max_posts: int = 5,
    dry_run: bool = False,
    mark_seen: bool = False,
    print_fn=print,
) -> int:
    """Forward new posts to recipient. Returns number of forwarded posts."""
    state_path = state_file or _default_state_file(client.config)
    seen_ids = load_forwarded_ids(state_path)

    client.ensure_authenticated()

    posts_to_process: List[Dict[str, Any]] = []

    if specific_post_id:
        post = client.post_detail(specific_post_id)
        if post and isinstance(post, dict):
            posts_to_process.append(post)
    else:
        inbox_data = client.inbox()
        raw_objects = inbox_data.get("objects", []) if isinstance(inbox_data, dict) else []
        for obj in raw_objects:
            post_id = str(obj.get("id"))
            if post_id and post_id not in seen_ids:
                posts_to_process.append(obj)

    if not posts_to_process:
        print_fn("Keine neuen Mitteilungen zum Weiterleiten.")
        return 0

    if mark_seen:
        for p in posts_to_process:
            pid = str(p.get("id"))
            if pid:
                seen_ids.add(pid)
        save_forwarded_ids(state_path, seen_ids)
        print_fn(f"{len(posts_to_process)} Mitteilung(en) als gesehen markiert.")
        return 0

    # Limit to max_posts, process oldest first
    if len(posts_to_process) > max_posts:
        posts_to_process = posts_to_process[:max_posts]
    posts_to_process.reverse()

    count = 0
    temp_dir = Path(tempfile.mkdtemp(prefix="edoop_forward_"))

    try:
        for post in posts_to_process:
            post_id = str(post.get("id"))
            title = post.get("name") or post_id
            print_fn(f"Verarbeite Post #{post_id}: {title}")

            attachment_paths: List[Path] = []
            docs = post.get("documents") or []
            for doc in docs:
                doc_id = str(doc.get("id"))
                if not doc_id:
                    continue
                try:
                    att_path = client.download_document(post_id, doc_id, destination=temp_dir)
                    attachment_paths.append(att_path)
                    print_fn(f"  Anhang heruntergeladen: {att_path.name}")
                except Exception as exc:
                    print_fn(f"  Warnung: Konnte Anhang {doc_id} nicht laden: {exc}")

            msg = build_email_message(
                post=post,
                recipient=recipient,
                from_header=from_header,
                attachment_paths=attachment_paths,
            )

            if dry_run:
                print_fn(f"  [DRY-RUN] Würde E-Mail an {recipient} senden:")
                print_fn(f"    Subject: {msg['Subject']}")
                print_fn(f"    From:    {msg['From']}")
                print_fn(f"    Anhänge: {[p.name for p in attachment_paths]}")
            else:
                dispatch_email(
                    msg=msg,
                    recipient=recipient,
                    mail_cmd=mail_cmd,
                    attachment_paths=attachment_paths,
                )
                seen_ids.add(post_id)
                save_forwarded_ids(state_path, seen_ids)
                print_fn(f"  E-Mail erfolgreich an {recipient} versendet.")

            count += 1
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return count
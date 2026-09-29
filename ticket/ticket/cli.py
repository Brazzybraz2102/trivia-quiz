"""`ticket` command line. Run `ticket --help`."""
from __future__ import annotations

import argparse
import getpass
import json
import mimetypes
import shutil
import subprocess
import sys
from pathlib import Path

from . import jobs, printer, scan
from .auth import Accounts, AuthError
from .config import load_settings
from .jobs import Context
from .store import Store


def _notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "ticket", title, body], check=False)


def _show(result: dict) -> None:
    status = result.get("status", "")
    if status == "skipped":
        print(f"skipped: {result['reason']}")
        return
    print(f"{status}: {result.get('kind')} #{result.get('id')} ({len(result.get('manifest', []))} rows)")
    print(f"png: {result.get('png')}")


def _user(accounts: Accounts, action: str, username: str | None) -> int:
    if action == "list":
        print("\n".join(accounts.list_users()) or "(no accounts)")
        return 0
    if not username:
        print("username required", file=sys.stderr)
        return 2
    try:
        if action == "remove":
            accounts.remove_user(username)
            print(f"removed {username}; their sessions are signed out")
            return 0
        pw = getpass.getpass(f"password for {username}: ")
        if pw != getpass.getpass("again: "):
            print("passwords don't match", file=sys.stderr)
            return 1
        accounts.set_password(username, pw, create=action == "add")
        print(f"{'created' if action == 'add' else 'updated'} {username}")
        return 0
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ticket", description="Todoist -> Brother QL label printer")
    ap.add_argument("--dry-run", action="store_true", help="render the PNG only; never touch the printer")
    ap.add_argument("--json", action="store_true", help="print raw JSON results")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="config + printer reachability")
    t = sub.add_parser("today", help="print today + overdue")
    t.add_argument("--auto", action="store_true", help="the once-a-day guarded print (Home Assistant)")
    lst = sub.add_parser("list", help="print any Todoist filter, e.g. '#Groceries' or 'p1'")
    lst.add_argument("query")
    lst.add_argument("--title")
    tk = sub.add_parser("task", help="print one task by id")
    tk.add_argument("task_id")
    tx = sub.add_parser("text", help="print a free-text note")
    tx.add_argument("text", nargs="+")
    tx.add_argument("--title", default="NOTE")
    tx.add_argument("--todo", action="store_true", help="also add it to Todoist")
    c = sub.add_parser("clip", help="print the clipboard (bind this to a hotkey)")
    c.add_argument("--todo", action="store_true", help="add clipboard to Todoist, then print its ticket")
    pr = sub.add_parser("printed", help="recent print history")
    pr.add_argument("-n", type=int, default=10)
    sc = sub.add_parser("scan", help="read back a photo of a marked-up label")
    sc.add_argument("photo", type=Path)
    sc.add_argument("--label", help="label code (#abc123) if the photo cuts it off")
    cf = sub.add_parser("confirm", help="review a scan's pending items one by one")
    cf.add_argument("scan_id")
    sub.add_parser("serve", help="run the HTTP server")
    us = sub.add_parser("user", help="manage web sign-in accounts")
    us.add_argument("action", choices=["add", "passwd", "remove", "list"])
    us.add_argument("username", nargs="?")

    args = ap.parse_args(argv)
    settings = load_settings()
    ctx = Context(settings=settings, store=Store(settings.data_dir))
    dry = args.dry_run or None

    try:
        if args.cmd == "status":
            out = {"printer_reachable": printer.is_reachable(settings.printer_ip), **settings.redacted()}
            print(json.dumps(out, indent=2))
            return 0 if out["printer_reachable"] else 1
        if args.cmd == "user":
            return _user(Accounts(settings.data_dir), args.action, args.username)
        if args.cmd == "serve":
            import uvicorn

            from .server import build_default_app

            uvicorn.run(build_default_app(), host=settings.host, port=settings.port)
            return 0
        if args.cmd == "printed":
            for r in ctx.store.list_printed(args.n):
                flag = " (dry)" if r["dry_run"] else ""
                print(f"#{r['id']}  {r['created_at']}  {r['kind']:<5} {r['source']:<6} {r['title'][:40]}{flag}")
            return 0
        if args.cmd == "scan":
            media = mimetypes.guess_type(args.photo.name)[0] or "image/jpeg"
            rec = scan.scan_photo(ctx, scan.claude_vision(settings), args.photo.read_bytes(), media,
                                  (args.label or "").lstrip("#") or None)
            print(json.dumps(rec, indent=2, ensure_ascii=False))
            if rec["needs_confirmation"]:
                print(f"\n{len(rec['needs_confirmation'])} item(s) need you: ticket confirm {rec['id']}")
            return 0
        if args.cmd == "confirm":
            rec = ctx.store.get_scan(args.scan_id)
            if not rec:
                print("no such scan", file=sys.stderr)
                return 1
            decisions = {}
            for item in rec["needs_confirmation"]:
                if item["status"] != "pending":
                    continue
                verb = {"drop": "DELETE", "done": "complete", "tomorrow": "move to tomorrow"}[item["mark"]]
                ans = input(f"row {item['row']}: {verb} '{item['content']}' "
                            f"(confidence {item['confidence']})? [y/N] ").strip().lower()
                decisions[item["row"]] = "confirm" if ans == "y" else "skip"
            rec = scan.confirm(ctx, args.scan_id, decisions)
            for item in rec["needs_confirmation"]:
                print(f"row {item['row']}: {item['status']}")
            return 0

        if args.cmd == "today":
            result = jobs.print_today(ctx, source="auto" if args.auto else "manual", dry_run=dry)
        elif args.cmd == "list":
            result = jobs.print_filter(ctx, args.query, args.title, dry_run=dry)
        elif args.cmd == "task":
            result = jobs.print_task(ctx, args.task_id, dry_run=dry)
        elif args.cmd == "text":
            text = " ".join(args.text)
            result = (jobs.add_and_print(ctx, text, dry_run=dry) if args.todo
                      else jobs.print_text(ctx, text, args.title, dry_run=dry))
        elif args.cmd == "clip":
            text = jobs.read_clipboard().strip()
            if not text:
                _notify("ticket", "Clipboard is empty")
                return 1
            result = (jobs.add_and_print(ctx, text, dry_run=dry) if args.todo
                      else jobs.print_text(ctx, text, "CLIP", dry_run=dry))
            _notify("ticket", f"{'Added + ' if args.todo else ''}{result['status']}: {text[:60]}")
        else:  # pragma: no cover
            ap.error(args.cmd)
    except Exception as exc:
        if args.cmd == "clip":
            _notify("ticket failed", str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False)) if args.json else _show(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())

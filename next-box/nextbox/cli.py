"""`nextbox` command line. Run `nextbox --help`."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

from . import jobs, printer, scan
from .auth import Accounts, AuthError, default_prefs
from .config import load_settings
from .connections import Connections
from .jobs import Context
from .mylist import LocalTasks, Tags, parse_dump
from .printers import Printers
from .providers import READY
from .store import Store
from .vault import Vault


def _notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "Next Box", title, body], check=False)


def _show(result: dict) -> None:
    status = result.get("status", "")
    if status == "skipped":
        print(f"skipped: {result['reason']}")
        return
    print(f"{status}: {result.get('kind')} #{result.get('id')} ({len(result.get('manifest', []))} rows)")
    print(f"png: {result.get('png')}")


def _user(accounts: Accounts, action: str, username: str | None, role: str | None = None) -> int:
    """The desktop is the recovery path: it works even if every web admin is locked out."""
    if action == "list":
        users = accounts.all_users()
        protected, owner = accounts.protected(), accounts.owner()
        for u in users:
            flags = " ".join(f for f, on in (("disabled", u["disabled"]), ("beta", u["beta"]),
                                             ("debug", u["debug"]), ("protected", u["username"] == protected),
                                             ("prints-for-home-assistant", u["username"] == owner)) if on)
            print(f"{u['username']:<20} {u['role']:<11} {flags}")
        if not users:
            print("(no accounts)")
        return 0
    if action == "unprotect":
        accounts.set_protected(None)
        print("no account is protected now")
        return 0
    if not username:
        print("username required", file=sys.stderr)
        return 2
    try:
        if action == "protect":
            accounts.set_protected(username)
            print(f"{username} is the protected admin: it always stays a superadmin and can't be removed "
                  "from the web app. Undo only here: nextbox user unprotect")
            return 0
        if action == "owner":
            accounts.set_print_owner(username)
            print(f"Home Assistant and `nextbox today` now print {username}'s tasks")
            return 0
        if action == "remove":
            accounts.remove_user(username)
            print(f"removed {username}; their sessions are signed out")
            return 0
        if action in {"role", "enable", "disable"}:
            if action == "role":
                if not role:
                    print("usage: nextbox user role <name> user|admin|superadmin", file=sys.stderr)
                    return 2
                accounts.update(username, role=role)
            else:
                accounts.update(username, disabled=action == "disable")
            u = accounts.get(username)
            print(f"{u['username']}: role={u['role']} disabled={u['disabled']}")
            return 0
        pw = getpass.getpass(f"password for {username}: ")
        if pw != getpass.getpass("again: "):
            print("passwords don't match", file=sys.stderr)
            return 1
        if action == "add":
            if role:
                accounts.create(username, pw, role=role)
            else:
                accounts.set_password(username, pw, create=True)  # first account -> superadmin
            print(f"created {username} ({accounts.get(username)['role']})")
        else:
            accounts.set_password(username, pw)
            print(f"updated {username}; their other devices are signed out")
        return 0
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _connect(conns: Connections, username: str, provider: str) -> int:
    if not conns.accounts.get(username):
        print(f"no user {username}", file=sys.stderr)
        return 1
    fields = {}
    for f in READY[provider]["fields"]:
        if f.get("help"):
            print(f"  {f['label']}: {f['help']}")
        prompt = f"{f['label']}{' (optional)' if f.get('optional') else ''}: "
        fields[f["name"]] = getpass.getpass(prompt) if f.get("secret") else input(prompt)
    try:
        status = conns.connect(username, provider, fields)
    except Exception as exc:
        print(f"couldn't connect: {exc}", file=sys.stderr)
        return 1
    print(f"{username}: connected to {status['name']} as {status.get('account')}")
    return 0


def _db(data_dir, args) -> int:
    from . import dbtools
    practice = getattr(args, "practice", False)
    try:
        if args.db_cmd == "where":
            loc = dbtools.describe_location(data_dir)
            practice_loc = dbtools.describe_location(dbtools.practice_url(data_dir))
            print(f"Real database:     {loc}")
            print(f"Practice database: {practice_loc}  (build it: nextbox db demo)")
            if loc.endswith(".db"):
                print("\nOpen it with:  sqlite3 -readonly '" + loc + "'   or the free app DB Browser for SQLite")
            else:
                print("\nThis is PostgreSQL. Open it with:  psql -h 127.0.0.1 -U nextbox nextbox")
                print("  (password: the one in DATABASE_URL in .env), or the free apps pgAdmin or DBeaver.")
            return 0
        if args.db_cmd == "copy-to":
            src = f"sqlite:///{Path(data_dir) / 'nextbox.db'}" if not args.source else args.source
            target = sys.stdin.read().strip() if args.url == "-" else args.url  # "-": read it from stdin
            counts = dbtools.copy_database(src, target)
            print("Copied: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v))
            print("Now set DATABASE_URL in .env to the new database and restart Next Box.")
            return 0
        if practice and not dbtools.practice_exists(data_dir):
            print("No practice database yet. Build it with: nextbox db demo", file=sys.stderr)
            return 1
        if args.db_cmd == "tables":
            for name, count, note in dbtools.tables(data_dir, practice):
                print(f"{name:<20} {count:>6} rows   {note}")
        elif args.db_cmd == "diagram":
            print(dbtools.DIAGRAM)
        elif args.db_cmd == "schema":
            print(dbtools.schema(data_dir, args.table, practice))
        elif args.db_cmd == "sql":
            cols, rows, note = dbtools.run_sql(data_dir, args.query, practice=practice, write=args.write)
            if cols:
                print(dbtools.format_table(cols, rows))
            print(f"\n({note})")
        elif args.db_cmd == "demo":
            counts = dbtools.build_practice(data_dir)
            print("Practice database ready:", dbtools.describe_location(dbtools.practice_url(data_dir)))
            print("  " + ", ".join(f"{k} {v}" for k, v in counts.items() if v))
            print('Try:  nextbox db sql --practice "SELECT username, role, household_id FROM users"')
        elif args.db_cmd == "backup":
            print("Saved", dbtools.backup(data_dir))
        return 0
    except PermissionError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # show SQL mistakes plainly; that's how you learn
        msg = str(getattr(exc, "orig", exc))
        if "readonly" in msg or "read-only" in msg:
            msg = ("that query changes data, and this is read-only. To practice changes use the practice "
                   "database: nextbox db sql --practice --write \"...\"")
        print(f"error: {msg}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nextbox", description="Your to-do list on a Brother QL label printer")
    ap.add_argument("--dry-run", action="store_true", help="render the PNG only; never touch the printer")
    ap.add_argument("--user", help="whose to-do app and tickets (default: the owner, i.e. first superadmin)")
    ap.add_argument("--json", action="store_true", help="print raw JSON results")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="config + printer reachability")
    t = sub.add_parser("today", help="print today + overdue")
    t.add_argument("--auto", action="store_true", help="the once-a-day guarded print (Home Assistant)")
    lst = sub.add_parser("list", help="print a Todoist filter ('#Groceries', 'p1') or a CalDAV list name")
    lst.add_argument("query")
    lst.add_argument("--title")
    tk = sub.add_parser("task", help="print one task by id")
    tk.add_argument("task_id")
    tx = sub.add_parser("text", help="print a free-text note")
    tx.add_argument("text", nargs="+")
    tx.add_argument("--title", default="NOTE")
    tx.add_argument("--todo", action="store_true", help="also add it to your to-do app")
    c = sub.add_parser("clip", help="print the clipboard (bind this to a hotkey)")
    c.add_argument("--todo", action="store_true", help="add clipboard to your to-do app, then print its ticket")
    pr = sub.add_parser("printed", help="recent print history")
    pr.add_argument("-n", type=int, default=10)
    sc = sub.add_parser("scan", help="read back a photo of a marked-up label")
    sc.add_argument("photo", type=Path)
    sc.add_argument("--label", help="label code (#abc123) if the photo cuts it off")
    cf = sub.add_parser("confirm", help="review a scan's pending items one by one")
    cf.add_argument("scan_id")
    sub.add_parser("serve", help="run the HTTP server")
    sub.add_parser("doctor", help="check the setup step by step and say what to fix")
    ad = sub.add_parser("add", help="add tasks to your list (one per line; #tag ! tomorrow 3pm ~15m)")
    ad.add_argument("text", nargs="+")
    for name, helptext in (("stickers", "one sticker per task (today's, or a filter like #errand)"),
                           ("strips", "one label of tear-off strips (today's, or a filter)")):
        x = sub.add_parser(name, help=helptext)
        x.add_argument("query", nargs="?", default=None)
    fo = sub.add_parser("focus", help="print Just one thing: the next task, big, with its tiny steps")
    fo.add_argument("task_id", nargs="?", default=None)
    cn = sub.add_parser("connect", help="connect someone's to-do app (prompts for the token/password)")
    cn.add_argument("username")
    cn.add_argument("provider", choices=sorted(READY))
    dc = sub.add_parser("disconnect", help="forget someone's to-do app connection")
    dc.add_argument("username")
    dbp = sub.add_parser("db", help="look inside the database and practice SQL (see DATABASE.md)")
    dbs = dbp.add_subparsers(dest="db_cmd", required=True)
    dbs.add_parser("where", help="where the database is and how to open it")
    for name, helptext in (("tables", "every table, its row count and what it's for"),
                           ("diagram", "how the tables connect")):
        x = dbs.add_parser(name, help=helptext)
        x.add_argument("--practice", action="store_true", help="the practice database instead of the real one")
    sch = dbs.add_parser("schema", help="the columns of one table")
    sch.add_argument("table")
    sch.add_argument("--practice", action="store_true")
    q = dbs.add_parser("sql", help="run a SQL query (read-only on the real database)")
    q.add_argument("query")
    q.add_argument("--practice", action="store_true", help="query the practice database")
    q.add_argument("--write", action="store_true", help="allow INSERT/UPDATE/DELETE (practice database only)")
    dbs.add_parser("demo", help="(re)build the practice database with realistic fake data")
    dbs.add_parser("backup", help="save a copy of the real database in the data folder's backups/")
    cp = dbs.add_parser("copy-to", help="copy everything into another (empty) database, e.g. PostgreSQL")
    cp.add_argument("url", help="postgresql://user:password@127.0.0.1/nextbox, or - to read it from stdin")
    cp.add_argument("--source", help="copy from this URL instead of the SQLite file in the data folder")
    iv = sub.add_parser("invite", help="make a one-time code so someone can create their own account")
    iv.add_argument("--days", type=int, default=7, help="how long the code works (1-90 days)")
    iv.add_argument("--own-household", action="store_true", help="they get their own household and printers")
    iv.add_argument("--beta", action="store_true", help="mark them as a beta tester")
    iv.add_argument("--note", default="", help="who it's for, e.g. 'Sam from work'")
    pn = sub.add_parser("prune", help="delete tickets and scans older than N days")
    pn.add_argument("--days", type=int, default=None)
    us = sub.add_parser("user", help="manage web sign-in accounts")
    us.add_argument("action", choices=["add", "passwd", "role", "enable", "disable", "remove", "list",
                                       "protect", "unprotect", "owner"])
    us.add_argument("username", nargs="?")
    us.add_argument("role", nargs="?", choices=["user", "admin", "superadmin"],
                    help="for `role` (and optionally `add`)")

    args = ap.parse_args(argv)
    os.umask(0o077)  # files Next Box writes stay private to this desktop user
    settings = load_settings()
    accounts = Accounts(settings.data_dir)
    conns = Connections(settings, accounts, Vault(settings.data_dir))
    who = (args.user or accounts.owner() or "cli").lower()
    ctx = Context(settings=settings, store=Store(settings.data_dir), user=who,
                  printers=Printers(settings.data_dir, settings),
                  tasks_factory=lambda: conns.provider_for(who),
                  prefs=(accounts.get(who) or {}).get("prefs") or default_prefs(),
                  tag_colors=Tags(accounts.db, who).colors() if accounts.get(who) else {})
    dry = args.dry_run or None

    if args.cmd == "db":
        return _db(settings.data_dir, args)
    if args.cmd == "invite":
        admin = accounts.protected() or accounts.owner() or "cli"
        try:
            code = accounts.create_invite(accounts.household_of(who) or "home", admin, days=args.days,
                                          beta=args.beta, note=args.note, own_household=args.own_household)
        except AuthError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"Invite code: {code}  (works once, for {args.days} days)")
        print(f"They open the web app, tap 'Have an invite code?', and enter it. Link: /app/#join={code}")
        return 0
    if args.cmd == "doctor":
        from .doctor import run_doctor
        return run_doctor(settings, accounts)
    if args.cmd == "add":
        if not accounts.get(who):
            print("Create your account first: nextbox user add <name>")
            return 1
        items = parse_dump("\n".join(args.text), date.today())
        mine = LocalTasks(accounts.db, who, accounts.household_of(who) or "home")
        for item in items:
            print(f"added {mine.create(item)['id']}: {item['content']}")
        return 0
    if args.cmd == "connect":
        return _connect(conns, args.username.lower(), args.provider)
    if args.cmd == "disconnect":
        conns.disconnect(args.username.lower())
        print(f"{args.username}: disconnected")
        return 0
    if args.cmd == "prune":
        print(ctx.store.prune(args.days or settings.retention_days))
        return 0

    try:
        if args.cmd == "status":
            out = {"printer_reachable": printer.is_reachable(settings.printer_ip), **settings.redacted()}
            print(json.dumps(out, indent=2))
            return 0 if out["printer_reachable"] else 1
        if args.cmd == "user":
            return _user(Accounts(settings.data_dir), args.action, args.username, args.role)
        if args.cmd == "serve":
            import uvicorn

            from .server import build_default_app

            # No access log: request lines (IP + time) would let someone match people to feedback.
            uvicorn.run(build_default_app(), host=settings.host, port=settings.port, access_log=False)
            return 0
        if args.cmd == "printed":
            for r in ctx.store.list_printed(args.n, by=who):
                flag = " (dry)" if r["dry_run"] else ""
                print(f"#{r['id']}  {r['created_at']}  {r['kind']:<5} {r['source']:<6} {r['title'][:40]}{flag}")
            return 0
        if args.cmd == "scan":
            data, media = scan.prepare_photo(args.photo.read_bytes())
            rec = scan.scan_photo(ctx, scan.claude_vision(settings), data, media,
                                  (args.label or "").lstrip("#") or None)
            print(json.dumps(rec, indent=2, ensure_ascii=False))
            if rec["needs_confirmation"]:
                print(f"\n{len(rec['needs_confirmation'])} item(s) need you: nextbox confirm {rec['id']}")
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
        elif args.cmd == "stickers":
            result = jobs.print_stickers(ctx, query=args.query, dry_run=dry)
        elif args.cmd == "strips":
            result = jobs.print_strips(ctx, query=args.query, dry_run=dry)
        elif args.cmd == "focus":
            result = jobs.print_focus(ctx, args.task_id, dry_run=dry)
        elif args.cmd == "clip":
            text = jobs.read_clipboard().strip()
            if not text:
                _notify("Next Box", "Clipboard is empty")
                return 1
            result = (jobs.add_and_print(ctx, text, dry_run=dry) if args.todo
                      else jobs.print_text(ctx, text, "CLIP", dry_run=dry))
            _notify("Next Box", f"{'Added + ' if args.todo else ''}{result['status']}: {text[:60]}")
        else:  # pragma: no cover
            ap.error(args.cmd)
    except Exception as exc:
        if args.cmd == "clip":
            _notify("Next Box failed", str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False)) if args.json else _show(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())

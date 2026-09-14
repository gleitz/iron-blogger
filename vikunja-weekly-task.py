#!/usr/bin/env python3
"""File the weekly recap as ONE task on a Vikunja list, idempotently.

Benjamin's Iron Blogger recap has landed in his Remember The Milk Inbox for
years, via a mail forward to an RTM address with a sender whitelist.  Vikunja
has no inbound mail-to-task at all (its mail is outbound only: reminders and
notifications; CalDAV is the only other write path) -- but no mail bridge is
needed, because the *sender* is ours.  This posts the recap directly, from the
same machine and the same run that sends it.

Run it as a POST-STEP of the weekly, never before the send::

    VIKUNJA_URL=https://… VIKUNJA_TOKEN=tk_… \\
        python3 vikunja-weekly-task.py --project 1 --date 2026-09-14 \\
                                       --body-file out/email.txt

The credential is read from the ENVIRONMENT and never from argv, because ``ps``
is readable by every user on a shared host.  Nothing host-specific is compiled
in: the URL, the project and the list's conventions are all arguments, which is
what lets this file live in a public repository.

THE GROUP MAIL IS THE PRODUCT
-----------------------------
Every failure here is reported and NONE of them may unwind, retry or delay the
recap.  The caller runs this only after the send has already returned 0, and
must keep the job green when this exits non-zero -- see ``jobs/weekly.sh``.

IDEMPOTENCY IS A READ THAT MAY REFUSE
-------------------------------------
The marker is an ``external_ref: <prefix>:<date>`` line written INTO the task's
description, which is the convention every other filer in this estate already
parses.  Vikunja has no uniqueness constraint on it, so the check is a read --
and a read that cannot answer is UNMEASURED, not "absent".  An unreadable list
REFUSES (exit 3) rather than creating, because a false "no task yet" here files
a duplicate of a 3 KB recap onto Benjamin's own Inbox, while a false refusal
files nothing and says so.  The two errors are not the same size.

The read and the create are held under a file lock (``--lock-file``, by
default beside the recap body), because they are two operations and not one:
two overlapping runs could otherwise both see absence and both file a card.

An exact TITLE match counts as present too.  Both records identify the same
week; the only failure that matters is a second row for one recap, and the
union catches it even if a description was edited in the app.

Exit codes
----------
0  the task is there -- ``created`` this run, or ``exists`` already
1  the create failed (the list was readable; Vikunja refused the write)
2  usage
3  UNMEASURED -- could not read the list, could not read the recap body, or
   could not hold the lock, so it will not guess.  NOT a 1.

One line of JSON goes to stdout in every case; diagnostics go to stderr, so a
caller can capture the verdict with ``>``.  No credential is ever printed.
"""
import argparse
import contextlib
import errno
import fcntl
import json
import os
import sys
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time
from zoneinfo import ZoneInfo

OK = 0
FAILED = 1
USAGE = 2
UNMEASURED = 3

#: One page of the list read.  Vikunja caps ``per_page`` server-side and says
#: so in ``x-pagination-total-pages``, which is what the loop below follows --
#: asking for more than the cap silently returns the cap, and a reader that
#: trusted its own number would stop one page short of a marker that is there.
PER_PAGE = 50

#: How long to wait for another run of this script to finish its own
#: check-and-create before giving up.  Generous on purpose: the thing being
#: waited for is two HTTP requests, and the cost of waiting is a few seconds in
#: a weekly job, while the cost of NOT waiting is a duplicate recap.
LOCK_WAIT_SECONDS = 120.0

#: A hard stop on the paging loop.  It is not a guess about list length: it is
#: the point past which "I read the whole list" stops being something this run
#: has evidence for, and the answer becomes UNMEASURED instead of "absent".
MAX_PAGES = 40


@contextlib.contextmanager
def exclusive(path, wait=LOCK_WAIT_SECONDS):
    """Hold ``path`` exclusively for the read-and-create, or refuse.

    Vikunja has no uniqueness constraint on the marker, so idempotency here is
    a read followed by a write and NOT one atomic operation: two overlapping
    runs can both read absence and both create (codex [P2] on 8f0e0815).  The
    weekly timer cannot overlap itself, but a hand re-run beside it can, and
    that is exactly when somebody is most likely to be running this twice.

    🪤 It does NOT fail open.  A lock that cannot be taken -- held too long, or
    a directory this process cannot write -- refuses, because the whole point
    of the lock is the case where another run is mid-create, and proceeding
    "because the guard was unavailable" is the duplicate it exists to stop.
    A guard that fails open reads as a pass.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise RuntimeError(
            "could not open the lock file %s (%s) -- pass --lock-file to put "
            "it somewhere writable" % (path, exc))
    deadline = _time.monotonic() + wait
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise RuntimeError("could not lock %s: %s" % (path, exc))
                if _time.monotonic() >= deadline:
                    raise RuntimeError(
                        "another run held %s for more than %.0fs -- refusing, "
                        "because it may be mid-create for this same date"
                        % (path, wait))
                _time.sleep(0.5)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _die(code, action, message, **extra):
    """Emit the one-line verdict and leave.  Always JSON, always one line."""
    row = {"action": action, "message": message}
    row.update(extra)
    sys.stdout.write(json.dumps(row, sort_keys=True) + "\n")
    sys.stdout.flush()
    raise SystemExit(code)


def marker_line(prefix, date):
    """The idempotency record, as its own line.

    ``external_ref: <prefix>:<date>`` -- the spelling every other reader in
    this estate already looks for, so a row filed here is legible to them
    without a second convention.
    """
    return "external_ref: %s:%s" % (prefix, date)


def compose_description(body_html, prefix, date):
    """The recap, unchanged, with the marker appended as its own paragraph.

    The body is passed through BYTE FOR BYTE: it is the same HTML the group
    receives, and deciding on Benjamin's behalf which parts of his own mail he
    wants is not this script's job.  Vikunja sanitises the HTML it stores; that
    is its call to make and it makes the same call for every writer.

    Appended rather than prepended so the first thing in the card is the recap.
    """
    return "%s\n<p>%s</p>\n" % (body_html.rstrip("\n"), marker_line(prefix, date))


def strip_tags(html):
    """Block-ish HTML -> lines, well enough to find a one-line marker.

    Deliberately crude and deliberately NOT an HTML parser: the only question
    asked of the result is whether some line equals the marker.  Closing block
    tags become newlines first so ``<p>external_ref: …</p><p>x</p>`` cannot be
    read as one line; then tags go, then entities.
    """
    import html as _html
    import re
    text = re.sub(r"</(p|div|li|ul|ol|h[1-6]|tr|table|blockquote|pre)>",
                  "\n", html or "", flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return _html.unescape(text)


def has_marker(description, marker):
    """True when some LINE of ``description`` is the marker.

    Line-anchored, not ``in``: the recap body is full of URLs and usernames,
    and a substring test would let any of them collide with a ref.
    """
    for line in strip_tags(description).splitlines():
        if line.strip() == marker:
            return True
    return False


class Vikunja:
    """The three requests this needs, and nothing else.

    The token travels in a header on each request and is never logged, never
    formatted into a URL and never placed on a command line.
    """

    def __init__(self, base, token, timeout=30.0):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method, path, payload=None):
        url = self.base + path
        data = None
        headers = {"Authorization": "Bearer " + self.token,
                   "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            # The header is the server's own opinion of how many pages exist.
            # Reading it back beats computing one from a count this client
            # never sees.
            pages = resp.headers.get("x-pagination-total-pages")
            return json.loads(raw) if raw.strip() else None, pages

    def tasks_in_project(self, project_id):
        """Every task on the project, DONE ONES INCLUDED.

        Done rows must be in the answer: a recap task Benjamin has already
        ticked is still a reason not to file a second one.  Measured on this
        route, with this token: a ``done: true`` row comes back.

        🪤 THE ROUTE IS ``GET /api/v1/tasks``, AND IT IS NOT ``/tasks/all``.
        Measured against Vikunja v2.6.0 on 2026-09-14 with a token scoped to
        ``tasks: [create, read_all]``:

            GET /api/v1/tasks?filter=project_id = 8   -> 200, rows
            GET /api/v1/tasks/all?filter=…            -> 401
            GET /api/v1/projects/8/tasks              -> 200, rows

        and the server's own ``GET /api/v1/routes`` names ``tasks.read_all``
        as ``{"path": "/api/v1/tasks", "method": "GET"}``.  A reviewer read
        this as a 404 against the older ``/tasks/all`` spelling (codex [P1] on
        8f0e0815) -- that spelling is the one that fails here, and swapping to
        it would have made every run UNMEASURED and filed nothing, forever,
        quietly.  Verify against ``/api/v1/routes`` before changing it.

        Raises on any failure.  The caller turns that into UNMEASURED; it must
        never turn into an empty list.
        """
        out = []
        page = 1
        while page <= MAX_PAGES:
            query = urllib.parse.urlencode({
                "page": page,
                "per_page": PER_PAGE,
                "filter": "project_id = %d" % project_id,
            })
            rows, pages = self._request("GET", "/api/v1/tasks?" + query)
            if not isinstance(rows, list):
                raise ValueError("task list page %d was not a list" % page)
            out.extend(rows)
            try:
                total = int(pages)
            except (TypeError, ValueError):
                raise ValueError("no x-pagination-total-pages on page %d "
                                 "-- cannot tell whether the list was read "
                                 "to the end" % page)
            if page >= total:
                return out
            page += 1
        raise ValueError("list did not end within %d pages" % MAX_PAGES)

    def create_task(self, project_id, payload):
        row, _ = self._request(
            "PUT", "/api/v1/projects/%d/tasks" % project_id, payload)
        if not isinstance(row, dict) or not row.get("id"):
            raise ValueError("create returned no task id")
        return row


def due_rfc3339(date, tz_name, offset_days):
    """Midnight local on the recap date (plus an optional offset), RFC3339.

    🪤 The due date is the date NAMED IN THE SUBJECT -- the Monday the recap
    settles -- not the Monday it is sent, and therefore a week in the past when
    the row appears.  That is not a bug and it is not a guess: it is what the
    RTM forward has produced for years, measured off the archived RTM export
    (a row added 2026-09-07T08:00:15Z carried due 2026-08-31T07:00:00Z, i.e.
    local midnight on the subject's own date).  Reproducing it is the whole
    ask.  ``--due-offset-days`` is the one knob if Benjamin ever wants the
    deadline instead of the label.
    """
    from datetime import timedelta
    day = datetime.strptime(date, "%Y-%m-%d").date() + timedelta(days=offset_days)
    local = datetime.combine(day, time(0, 0, 0), tzinfo=ZoneInfo(tz_name))
    return local.isoformat()


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="File the weekly recap as one idempotent Vikunja task.")
    p.add_argument("--project", type=int, required=True,
                   help="Vikunja project id to file into")
    p.add_argument("--date", required=True,
                   help="the recap date, YYYY-MM-DD (the one in the subject)")
    p.add_argument("--body-file", required=True,
                   help="the recap HTML, exactly as mailed (out/email.txt)")
    p.add_argument("--ref-prefix", default="iron-blogger",
                   help="external_ref namespace (default: iron-blogger)")
    p.add_argument("--title-prefix", default="[iron-blogger] Weekly Recap",
                   help="task title, before the date")
    p.add_argument("--due-tz", default="America/Los_Angeles",
                   help="timezone the due date's midnight is taken in")
    p.add_argument("--due-offset-days", type=int, default=0,
                   help="days to shift the due date off the recap date "
                        "(default 0 -- what the RTM forward produced)")
    p.add_argument("--lock-file", default=None,
                   help="serialise the check-and-create on this path "
                        "(default: <body-file>.lock)")
    p.add_argument("--dry-run", action="store_true",
                   help="read and report, create nothing")
    return p.parse_args(argv)


def main(argv):
    args = parse_args(argv)

    # 🪤 CANONICALISE, do not merely validate. ``strptime`` accepts
    # ``2026-9-1`` as happily as ``2026-09-01``, and the raw spelling used to
    # flow straight into the title and the marker -- so a later run using the
    # documented spelling would match neither and file a SECOND card for the
    # same week (codex [P2] on 8f0e0815). The parsed date's own ``isoformat``
    # is the single spelling everything downstream sees.
    try:
        date = datetime.strptime(args.date, "%Y-%m-%d").date().isoformat()
    except ValueError:
        _die(USAGE, "usage", "--date must be YYYY-MM-DD, got %r" % args.date)

    base = (os.environ.get("VIKUNJA_URL") or "").strip()
    token = (os.environ.get("VIKUNJA_TOKEN") or "").strip()
    if not base.lower().startswith(("http://", "https://")):
        _die(USAGE, "usage",
             "VIKUNJA_URL must be set in the environment (http[s]://host)")
    if not token:
        _die(USAGE, "usage",
             "VIKUNJA_TOKEN must be set in the environment -- never on argv")

    try:
        with open(args.body_file, "r", encoding="utf-8") as fh:
            body = fh.read()
    except OSError as exc:
        _die(UNMEASURED, "unmeasured",
             "could not read the recap body %s: %s" % (args.body_file, exc))
    if not body.strip():
        # An empty body would file a card with nothing in it and report
        # success, which is worse than not filing.
        _die(UNMEASURED, "unmeasured",
             "the recap body %s is empty" % args.body_file)

    marker = marker_line(args.ref_prefix, date)
    title = "%s %s" % (args.title_prefix, date)
    ref = "%s:%s" % (args.ref_prefix, date)
    api = Vikunja(base, token)
    lock_path = args.lock_file or (args.body_file + ".lock")

    try:
        # The read and the create are ONE critical section. Everything from
        # here to the create is inside it, because the window this closes is
        # exactly the gap between them.
        with exclusive(lock_path):
            _file_it(api, args, marker=marker, title=title,
                     ref=ref, body=body, date=date)
    except RuntimeError as exc:
        # Only the lock raises RuntimeError here. `_die` raises SystemExit,
        # which is not an Exception and passes straight through -- so a verdict
        # reached inside the section is never re-read as a lock failure.
        _die(UNMEASURED, "unmeasured", str(exc))

    # Unreachable: `_file_it` always leaves through `_die`. Here so that a
    # future edit which forgets that cannot exit 0 with nothing on stdout,
    # which a caller would read as a filed task.
    _die(UNMEASURED, "unmeasured",
         "internal: the check-and-create returned without a verdict")


def _file_it(api, args, *, marker, title, ref, body, date):
    """The check-and-create, run under the lock. Always leaves via ``_die``."""
    try:
        rows = api.tasks_in_project(args.project)
    except Exception as exc:                      # noqa: BLE001 -- see below
        # EVERY read failure lands here on purpose, and every one of them is
        # UNMEASURED.  Distinguishing a 401 from a timeout would change the
        # wording and not the verdict: neither one is evidence that the task
        # is absent, and absence is the only thing that licenses a create.
        _die(UNMEASURED, "unmeasured",
             "could not read project %d: %s: %s"
             % (args.project, type(exc).__name__, exc))

    for row in rows:
        if not isinstance(row, dict):
            continue
        if has_marker(row.get("description") or "", marker):
            _die(OK, "exists", "already filed (external_ref)",
                 task_id=row.get("id"), ref=ref)
        if (row.get("title") or "").strip() == title:
            _die(OK, "exists", "already filed (title)",
                 task_id=row.get("id"), ref=ref)

    payload = {
        "title": title,
        "description": compose_description(body, args.ref_prefix, date),
        "due_date": due_rfc3339(date, args.due_tz, args.due_offset_days),
    }

    if args.dry_run:
        _die(OK, "would-create", "dry run: no task was created",
             ref=ref,
             title=title, due_date=payload["due_date"],
             description_bytes=len(payload["description"]),
             scanned=len(rows))

    try:
        made = api.create_task(args.project, payload)
    except Exception as exc:                      # noqa: BLE001
        _die(FAILED, "failed",
             "create in project %d refused: %s: %s"
             % (args.project, type(exc).__name__, exc))

    _die(OK, "created", "filed",
         task_id=made.get("id"), ref=ref,
         title=title, due_date=made.get("due_date"))


if __name__ == "__main__":
    main(sys.argv[1:])

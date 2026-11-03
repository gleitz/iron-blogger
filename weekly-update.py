#!/usr/bin/python
import sys
import xmlrpclib
import subprocess
import yaml
import smtplib
import json

from datetime import datetime
print 'Weekly update started at ' + str(datetime.now())

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import render
from config import *


def _git(args, label="git"):
    """Best-effort git.

    The ported job runs in a container with NO git binary and the app dir is not
    a repo, so an un-caught git failure aborted the whole weekly BEFORE the email
    was sent (proved 2026-08-24). The ledger/bloggers.yml writes themselves are
    plain file writes and still happen; only the version-control step is skipped.
    """
    try:
        subprocess.check_call(["git"] + args)
        return True
    except Exception as e:
        print 'NOTE: git %s skipped, as expected on 143 (not a checkout, no git in the image; %s: %s) -- the ledger is written and the email still sends' % (
            args[0], type(e).__name__, e)
        return False

def _ledger_week_marker(date):
    """The line-prefix the ledger uses for the week that a run for `date` settles.

    THE LEDGER LABELS EACH ENTRY WITH THE WEEK-END DATE -- `date` + 7 days -- NOT
    `date` itself. Proven against render: date=2026-08-24 renders
    '2026-08-31 Fine Week 673'. Keying the guard on `date` instead would match the
    entry written by the PREVIOUS week's run and refuse every legitimate weekly
    whenever the prior week had fines. Computed here from render's OWN week
    arithmetic so the guard and the writer cannot drift apart.
    """
    w = render.to_week_num(date)
    week_end = START + ((w + 1) * render.datetime.timedelta(7))
    return week_end.strftime("%Y-%m-%d") + " Fine Week"


def _ledger_has_week(path, date):
    """True if `path` already carries the ledger entry a run for `date` would write.

    Guards the duplicate-week path: the ledger is opened in APPEND mode with no
    idempotency check, so a second run for the same date appends a duplicate week
    of real debts, and the punt branch then recomputes from the doubled ledger and
    can expel bloggers who do not owe that much.

    Any read/parse failure PROPAGATES -- the caller MUST refuse, never assume the
    date is absent. An unreadable ledger read as "no entry yet" would be a null
    from an instrument never shown capable of a hit, sitting directly on top of
    the duplicate-debt path.

    Known and ACCEPTED gap: a week in which nobody was fined renders no dated line,
    so a duplicate run of such a week is undetectable here. That is deliberate, not
    an oversight -- such a run appends no debts, leaves get_debts() unchanged and
    produces an identical punt branch, so its only residual harm is a duplicate
    email and blog post. COVERAGE IS CO-EXTENSIVE WITH THE FINANCIAL RISK, which is
    the right test for this guard: not "does it catch everything" but "does it catch
    everything that costs".
    """
    marker = _ledger_week_marker(date)
    with open(path) as f:
        for line in f:
            if line.startswith(marker):
                return True
    return False


def send_email(address, html, date):
    with open('secret.txt', 'r') as f:
        secret = json.load(f)
        username = secret['gmail']['username']
        password = secret['gmail']['password']

    print 'Sending weekly email to {0}...'.format(address)
    smtp = smtplib.SMTP('smtp.gmail.com', 587)
    smtp.ehlo()
    smtp.starttls()
    smtp.ehlo()
    smtp.login(username, password)
    msg = MIMEMultipart('mixed')
    msg['Subject'] = 'Weekly Recap for ' + date
    msg['To'] = address
    msg['From'] = username
    msg['Content-Type'] = 'text/html'
    msg.attach(MIMEText(html, 'html'))
    smtp.sendmail(username, address, msg.as_string())

    print 'Weekly email send successfully'
    smtp.close()


def generate_email():
    dry_run = False

    args = sys.argv[1:]
    if args[0] == '-n':
        dry_run = True
        args = args[1:]

    date = args[0]

    # --- duplicate-week refusal (ADDITIVE: decides only WHETHER to proceed;
    #     changes nothing about what proceeding does) ---
    try:
        _dup = _ledger_has_week('ledger', date)
    except Exception as _e:
        sys.stderr.write(
            "REFUSING: cannot read the ledger (%s: %s). Refusing rather than risk "
            "appending a duplicate week for %s. NOTHING was appended, posted or "
            "sent. This is a REFUSAL, not a failure.\n" % (type(_e).__name__, _e, date))
        sys.exit(3)
    if _dup:
        sys.stderr.write(
            "REFUSING: the ledger already carries the entry for the week settled by "
            "%s (marker %r) -- this week has already been run. NOTHING was appended, "
            "posted or sent; no debts were changed and no blogger was punted. "
            "This is a REFUSAL, not a failure.\n" % (date, _ledger_week_marker(date)))
        sys.exit(2)

    _snap = {}
    if dry_run:
        for _f in ("ledger", "bloggers.yml"):
            try:
                with open(_f) as _s:
                    _snap[_f] = _s.read()
            except IOError:
                pass

    with open('ledger', 'a') as f:
        f.write("\n")
        f.write(render.render_template('templates/ledger', date))

    if not dry_run:
        _git([ "commit", "ledger",
                               "-m", "Update for %s" % (date,)], "git")

    debts = render.get_debts()
    punt = []

    with open('ledger', 'a') as f:
        f.write("\n")
        for (user, debt) in debts:
            if debt <= (FINE_SIZE * 6):
                continue
            punt.append(user)
            f.write(PUNT_TEXT % {'user': user, 'debt': debt, 'date': date})


    if not dry_run:
        text = render.render_template('templates/week.tmpl', date, punt=punt)

        lines = text.split("\n")
        title = lines[0]
        body  = "\n".join(lines[1:])

        page = dict(title = title, description = body)

        with open('secret.txt', 'r') as f:
            secret = json.load(f)
            passwd = secret['wordpress']['password']

        try:
            x = xmlrpclib.ServerProxy(XMLRPC_ENDPOINT)
            x.metaWeblog.newPost(BLOG_ID, USER, passwd, page, True)
        except:
            # website is down?
            pass


    email = render.render_template('templates/email.html', date, punt=punt)

    if dry_run:
        print email
    else:
        send_email('iron-blogger-sf@googlegroups.com', email, date)

    with open('out/email.txt', 'w') as f:
        f.write(email)

    if punt:
        with open('bloggers.yml') as b:
            bloggers = yaml.safe_load(b)
        for p in punt:
            if bloggers.get(p) and 'end' not in bloggers[p]:
                bloggers[p]['end'] = date
        with open('bloggers.yml','w') as b:
            yaml.safe_dump(bloggers, b)

        if not dry_run:
            _git([ "commit", "ledger", "bloggers.yml",
                                   "-m", "Punts for %s" % (date,)], "git")

    # if it's a dry run, lets set the ledger back to the beginning state.
    # (git checkout is unavailable on the kibi guest, so restore from snapshot.)
    if dry_run:
        for _f, _txt in _snap.items():
            with open(_f, 'w') as _o:
                _o.write(_txt)
        print 'dry-run: restored %s from snapshot (no git needed)' % ', '.join(sorted(_snap))

if __name__ == '__main__':
    generate_email()

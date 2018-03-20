#!/usr/bin/python
import yaml
import feedparser
import datetime
import os
import sys
from dateutil.parser import parse
import dateutil.tz as tz

with open('bloggers.yml') as f:
    users = yaml.safe_load(f.read())

try:
    if not os.path.exists('out'):
        os.makedirs('out') 
    with open('out/report.yml') as f:
        log = yaml.safe_load(f.read())
except IOError:
    log = {}

from config import START

def parse_published(pub):
    return parse(pub).astimezone(tz.tzlocal()).replace(tzinfo=None)

def get_date(post):
    for k in ('published', 'created', 'updated'):
        if k in post:
            return post[k]

def get_link(post):
    return post.link

def parse_feeds(weeks, uri):
    feed = feedparser.parse(uri)

    print >>sys.stderr, "Parsing: %s" % uri

    if not feed.entries:
        print >>sys.stderr, "WARN: no entries for ", uri
    for post in feed.entries:
        try:
            date = parse_published(get_date(post))
        except ValueError:
            print >>sys.stderr, "ERROR: cannot parse date"
            continue
        if date < START:
            continue
        wn = (date - START).days / 7

        while len(weeks) <= wn:
            weeks.append([])

        post = dict(date=date,
                    title=post.title,
                    url=get_link(post))
        if post['url'] not in [p['url'] for p in weeks[wn]]:
            weeks[wn].append(post)

if len(sys.argv) > 1:
    for username in sys.argv[1:]:
        weeks = log.setdefault(username, [])
        for l in users[username]['links']:
            parse_feeds(weeks, l[2])
else:
    for (username, u) in users.items():
        weeks = log.setdefault(username, [])
        for l in u['links']:
            if len(l) >= 3:
                parse_feeds(weeks, l[2])

with open('out/report.yml', 'w') as f:
    yaml.safe_dump(log, f)

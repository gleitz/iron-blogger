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

    with open('ledger', 'a') as f:
        f.write("\n")
        f.write(render.render_template('templates/ledger', date))

    if not dry_run:
        subprocess.check_call(["git", "commit", "ledger",
                               "-m", "Update for %s" % (date,)])

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
            subprocess.check_call(["git", "commit", "ledger", "bloggers.yml",
                                   "-m", "Punts for %s" % (date,)])

    # if it's a dry run, lets set the ledger back to the beginning state
    if dry_run:
        subprocess.check_call(["git", "checkout", "ledger"])

        if punt:
            subprocess.check_call(["git", "checkout", "bloggers.yml"])

if __name__ == '__main__':
    generate_email()

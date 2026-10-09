# -*- coding: utf-8 -*-
"""SMTP 邮件推送: 从 stdin 读 JSON {host,port,user,pass,to,subject,body}, 结果 JSON 打到 stdout"""
import sys
import json
import smtplib
from email.mime.text import MIMEText
from email.header import Header


def main():
    try:
        cfg = json.loads(sys.stdin.read())
        host = cfg.get("host") or "smtp.qq.com"
        port = int(cfg.get("port") or 465)
        user = cfg["user"]
        pwd = cfg["pass"]
        to = cfg["to"]
        subject = cfg.get("subject") or "(无主题)"
        body = cfg.get("body") or ""
        if not (user and pwd and to):
            print(json.dumps({"ok": False, "err": "缺少发件账号/授权码/收件邮箱"}))
            return
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = user
        msg["To"] = to
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            smtp = smtplib.SMTP(host, port, timeout=20)
            smtp.starttls()
        smtp.login(user, pwd)
        smtp.sendmail(user, [to], msg.as_string())
        smtp.quit()
        print(json.dumps({"ok": True}))
    except Exception as e:
        print(json.dumps({"ok": False, "err": str(e)[:300]}))


if __name__ == "__main__":
    main()

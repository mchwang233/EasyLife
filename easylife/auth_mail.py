"""Account e-mails using the reminder service's SMTP configuration and sender."""

import email.utils
import logging
import urllib.parse
from email.message import EmailMessage

from easylife import auth, mail_settings, reminders

log = logging.getLogger("easylife.auth_mail")


def send(conn, user, purpose, cfg, *, mail_config=None):
    """Issue a token and send it; never return its plaintext to an API caller."""
    verify = purpose == "verify"
    ttl = auth.VERIFY_TOKEN_TTL if verify else auth.RESET_TOKEN_TTL
    token = auth.issue_token(conn, user["id"], purpose, ttl)
    try:
        public_url = (cfg.get("public_url") or cfg["base_url"]).rstrip("/")
        link = "%s/#/%s?%s" % (public_url, "verify" if verify else "reset",
                              urllib.parse.urlencode({"token": token}))
        effective = (mail_settings.resolve_mail_config(conn, cfg)
                     if mail_config is None else mail_config)
        smtp = reminders.normalize_config(effective)
        msg = EmailMessage()
        msg["Subject"] = "[EasyLife] " + ("验证邮箱" if verify else "重置密码")
        msg["From"] = smtp["from"]
        msg["To"] = user["email"]
        msg["Date"] = email.utils.formatdate(localtime=False)
        msg["Message-ID"] = email.utils.make_msgid(domain="easylife")
        msg.set_content("请打开以下链接%s：\n%s\n\n链接 %s 内有效，只能使用一次。"
                        "\n如果不是你发起的请求，请忽略本邮件。\n" % (
                            "验证邮箱" if verify else "重置密码", link,
                            "24 小时" if verify else "1 小时"))
        if not effective["enabled"]:
            # Explicit development / no-SMTP fallback: allow login and write the
            # entire mail to the server log, with a WARNING. Never silently pretend
            # it was delivered. Configured-but-broken SMTP does NOT bypass verification.
            log.warning("SMTP disabled: 认证邮件仅写入服务器日志；邮箱验证不阻止登录。"
                        " to=%s subject=%s\n%s", auth.mask_email(user["email"]),
                        msg["Subject"], msg.get_content())
        return reminders.send_message(smtp, msg)
    except Exception as exc:
        # Keep forgot-password's response identical for transport AND message
        # construction failures (e.g. an invalid configured SMTP_FROM header).
        # Exception text / SMTP transcripts may contain secrets, so log only type.
        log.error("认证邮件发送失败 purpose=%s to=%s error=%s", purpose,
                  auth.mask_email(user["email"]), type(exc).__name__)
        return "failed"

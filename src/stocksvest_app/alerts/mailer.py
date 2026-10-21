"""Sending email through the SMTP relay (Brevo), STARTTLS on port 587."""

import logging
import smtplib
import ssl
from email.message import EmailMessage

from ..config import Settings

log = logging.getLogger(__name__)


class Mailer:
    """Sends plain-text email to the alert recipient.

    Attributes:
        settings: smtp_*, alert_email_from, alert_email_to.

    Used by: deps.AppContext (ctx.mailer), alerts.checks.
    """

    def __init__(self, settings: Settings):
        """Args:
            settings: app settings.
        """
        self.settings = settings

    @property
    def configured(self) -> bool:
        """Whether the relay, its login and a recipient are set."""
        s = self.settings
        return bool(s.smtp_host and s.smtp_username and s.smtp_password and s.alert_email_to)

    def send(self, subject: str, text: str) -> None:
        """Send one email to ALERT_EMAIL_TO.

        Args:
            subject: subject line (the "[StocksVest]" prefix is added).
            text: plain-text body.

        Raises:
            smtplib.SMTPException, OSError: the relay refused or could not be reached.
        """
        s = self.settings
        msg = EmailMessage()
        msg["From"] = s.alert_email_from
        msg["To"] = s.alert_email_to
        msg["Subject"] = f"[StocksVest] {subject}"
        msg.set_content(text)
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(msg)
        log.info("alert email sent: %s", subject)

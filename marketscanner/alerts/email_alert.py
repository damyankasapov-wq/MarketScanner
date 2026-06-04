import io
import logging
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytz

from marketscanner import config
from marketscanner.strategies.base import Signal

log = logging.getLogger(__name__)

_ET = pytz.timezone(config.TIMEZONE)


def send_alert(signal: Signal, chart_figure=None) -> None:
    fired_et = signal.fired_at.astimezone(_ET).strftime("%Y-%m-%d %H:%M:%S %Z")
    subject = (
        f"[MarketScanner] {signal.market} {signal.strategy} "
        f"{signal.direction} @ {signal.price:.2f}"
    )
    body_lines = [
        f"Market:    {signal.market}",
        f"Strategy:  {signal.strategy}",
        f"Direction: {signal.direction}",
        f"Price:     {signal.price:.2f}",
        f"Time (ET): {fired_et}",
    ]
    if signal.box_top is not None:
        body_lines.append(f"Box top:   {signal.box_top:.2f}")
    if signal.box_bottom is not None:
        body_lines.append(f"Box bot:   {signal.box_bottom:.2f}")

    body = "\n".join(body_lines)

    cfg = config.EMAIL
    if not cfg["sender"] or not cfg["password"] or not cfg["recipient"]:
        log.warning("Email not configured — signal logged but not sent: %s", signal)
        return

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = cfg["sender"]
    msg["To"] = cfg["recipient"]
    msg.attach(MIMEText(body, "plain"))

    if chart_figure is not None:
        buf = io.BytesIO()
        chart_figure.savefig(buf, format="png", bbox_inches="tight")
        buf.seek(0)
        img = MIMEImage(buf.read(), name="chart.png")
        img.add_header("Content-Disposition", "inline", filename="chart.png")
        msg.attach(img)

    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(cfg["sender"], cfg["password"])
            smtp.sendmail(cfg["sender"], cfg["recipient"], msg.as_string())
        log.info("Alert sent: %s", subject)
    except Exception as exc:
        log.error("Failed to send alert: %s", exc)

#!/usr/bin/env python3
"""Notify subscribers when the Referenzzinssatz drops — mailbox-as-database.

There is no subscriber list stored anywhere: the info@zinscheck.ch mailbox IS
the list. People subscribe by sending a mail with subject "ABO" (the site's
button pre-fills it) and unsubscribe with subject "STOP"; per address, the
most recent instruction wins. This script scans the mailbox over IMAP, then
sends one localized plain-text mail per subscriber over Infomaniak SMTP.

The repo and its Action logs are public, so NO email address is ever printed
— only counts.

Credentials come from the environment (GitHub Actions secrets):
  MAIL_USER      full mailbox address, e.g. info@zinscheck.ch
  MAIL_PASSWORD  mailbox (or app) password

Modes:
  --selftest     offline checks of parsing + all four templates (no network)
  --list         connect to IMAP, print subscriber COUNTS per language
  --test ADDR    send one test mail per language to ADDR only (uses the most
                 recent decrease in the series, skips the freshness gate)
  --send         the real thing — refuses unless the newest series entry is a
                 decrease that took effect within the last 14 days

Exit codes: 0 = ok · 2 = refused/failed (nothing or only partly sent).
"""

import email.utils
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mietrecht_engine import reference_rate_change_pct  # noqa: E402

PAGE = ROOT / "index.html"
IMAP_HOST = "mail.infomaniak.com"
SMTP_HOST = "mail.infomaniak.com"
SITE = "https://zinscheck.ch"
SENDER_NAME = "Zinscheck"
# Scanned in order; folders that don't exist are skipped, so ABO mails may be
# tidied away into a folder called "Abo" without losing the subscription.
FOLDERS = ("INBOX", "Abo", "INBOX/Abo")
SEND_DELAY_S = 1.5          # stay far inside Infomaniak's 1440-mails/24h limit
FRESH_DAYS = 14             # --send refuses if the decrease is older than this

SUBSCRIBE_RE = re.compile(r"\b(ABO|ABONNIEREN|SUBSCRIBE)\b", re.I)
UNSUBSCRIBE_RE = re.compile(r"\b(STOP|ABMELDEN|UNSUBSCRIBE)\b", re.I)
LANG_RE = re.compile(r"\[(DE|FR|IT|EN)\]", re.I)
# bounce daemons must never end up on the list (bounces quote our subject)
ROBOT_RE = re.compile(r"^(mailer-daemon|postmaster|no-?reply|bounce)", re.I)

TEMPLATES = {
    "de": {
        "subject": "Referenzzinssatz gesunken auf {new} % — Mietzinssenkung prüfen",
        "body": (
            "Guten Tag\n"
            "\n"
            "Der hypothekarische Referenzzinssatz ist per {eff} von {old} % auf {new} % "
            "gesunken (Publikation des Bundesamts für Wohnungswesen).\n"
            "\n"
            "Beruht Ihre Miete auf {old} %, können Sie eine Senkung von rund {pct} % "
            "verlangen — beruht sie auf einem höheren Satz, entsprechend mehr.\n"
            "\n"
            "Auf {site} berechnen Sie Ihr Senkungspotenzial in Sekunden und erhalten die "
            "passende Briefvorlage für das Begehren an Ihre Verwaltung.\n"
            "\n"
            "Freundliche Grüsse\n"
            "Zinscheck\n"
            "\n"
            "--\n"
            "Sie erhalten diese E-Mail, weil Sie sich auf zinscheck.ch angemeldet haben.\n"
            "Abmelden: Antwort mit Betreff «STOP» genügt.\n"
        ),
    },
    "fr": {
        "subject": "Taux de référence abaissé à {new} % — vérifiez votre loyer",
        "body": (
            "Bonjour,\n"
            "\n"
            "Le taux d'intérêt de référence hypothécaire est passé de {old} % à {new} % "
            "au {eff} (publication de l'Office fédéral du logement).\n"
            "\n"
            "Si votre loyer repose sur {old} %, vous pouvez demander une baisse d'environ "
            "{pct} % — davantage s'il repose sur un taux plus élevé.\n"
            "\n"
            "Sur {site}, calculez votre potentiel de baisse en quelques secondes et "
            "obtenez le modèle de lettre pour la demande à votre gérance.\n"
            "\n"
            "Meilleures salutations,\n"
            "Zinscheck\n"
            "\n"
            "--\n"
            "Vous recevez cet e-mail parce que vous vous êtes inscrit·e sur zinscheck.ch.\n"
            "Désinscription : répondez avec l'objet « STOP ».\n"
        ),
    },
    "it": {
        "subject": "Tasso di riferimento sceso a {new} % — verifichi la pigione",
        "body": (
            "Buongiorno,\n"
            "\n"
            "Il tasso d'interesse di riferimento ipotecario è sceso dal {old} % al {new} % "
            "con effetto dal {eff} (pubblicazione dell'Ufficio federale delle abitazioni).\n"
            "\n"
            "Se la Sua pigione si basa sul {old} %, può chiedere una riduzione di circa "
            "{pct} % — di più se si basa su un tasso superiore.\n"
            "\n"
            "Su {site} calcola in pochi secondi il Suo potenziale di riduzione e ottiene "
            "il modello di lettera per la richiesta alla Sua amministrazione.\n"
            "\n"
            "Cordiali saluti\n"
            "Zinscheck\n"
            "\n"
            "--\n"
            "Riceve questa e-mail perché si è iscritto/a su zinscheck.ch.\n"
            "Disiscrizione: risponda con oggetto «STOP».\n"
        ),
    },
    "en": {
        "subject": "Reference rate cut to {new} % — check your rent",
        "body": (
            "Hello,\n"
            "\n"
            "The Swiss mortgage reference interest rate dropped from {old} % to {new} % "
            "effective {eff} (published by the Federal Office for Housing).\n"
            "\n"
            "If your rent is based on {old} %, you can request a reduction of about "
            "{pct} % — more if it is based on a higher rate.\n"
            "\n"
            "On {site} you can calculate your reduction potential in seconds and get the "
            "letter template for the request to your landlord.\n"
            "\n"
            "Kind regards,\n"
            "Zinscheck\n"
            "\n"
            "--\n"
            "You receive this email because you signed up on zinscheck.ch.\n"
            "Unsubscribe: reply with the subject \"STOP\".\n"
        ),
    },
}

DATE_FMT = {"de": "%d.%m.%Y", "fr": "%d.%m.%Y", "it": "%d.%m.%Y", "en": "%d.%m.%Y"}


def fail(msg: str) -> None:
    print(f"NOTIFY FAILED: {msg}", file=sys.stderr)
    sys.exit(2)


def fmt_rate(r: float) -> str:
    return f"{r:.2f}"


# ---------------------------------------------------------------------------
# Rate data — read from the same JSON block the page renders from
# ---------------------------------------------------------------------------

def load_series() -> list[tuple[date, float]]:
    m = re.search(
        r'<script id="rate-data" type="application/json">\s*(\{.*?\})\s*</script>',
        PAGE.read_text(encoding="utf-8"),
        re.S,
    )
    if not m:
        fail("rate-data block not found in index.html")
    entries = json.loads(m.group(1))["series"]
    if len(entries) < 2:
        fail("rate series too short")
    return [(date.fromisoformat(d), float(r)) for d, r in entries]


def build_mail(lang: str, sender: str, to: str, old: float, new: float, eff: date) -> EmailMessage:
    t = TEMPLATES[lang]
    pct = abs(reference_rate_change_pct(old, new)) * 100
    vals = {
        "old": fmt_rate(old),
        "new": fmt_rate(new),
        "pct": f"{pct:.2f}",
        "eff": eff.strftime(DATE_FMT[lang]),
        "site": SITE,
    }
    msg = EmailMessage()
    msg["From"] = f"{SENDER_NAME} <{sender}>"
    msg["To"] = to
    msg["Subject"] = t["subject"].format(**vals)
    msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=STOP>"
    msg.set_content(t["body"].format(**vals))
    return msg


# ---------------------------------------------------------------------------
# Mailbox scan — build {address: language} from ABO/STOP mails
# ---------------------------------------------------------------------------

def classify(subject: str) -> str | None:
    """'sub', 'unsub', or None. STOP wins if a subject somehow contains both."""
    if UNSUBSCRIBE_RE.search(subject):
        return "unsub"
    if SUBSCRIBE_RE.search(subject):
        return "sub"
    return None


def pick_lang(subject: str) -> str:
    m = LANG_RE.search(subject)
    return m.group(1).lower() if m else "de"


def decode_subject(raw: str) -> str:
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def collect_subscribers(user: str, password: str) -> dict[str, str]:
    imap = imaplib.IMAP4_SSL(IMAP_HOST, 993, ssl_context=ssl.create_default_context())
    imap.login(user, password)
    # (last_action_time, action, lang) per lower-cased address
    state: dict[str, tuple[datetime, str, str]] = {}
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        for folder in FOLDERS:
            ok, _ = imap.select(f'"{folder}"', readonly=True)
            if ok != "OK":
                continue
            ok, data = imap.search(None, "ALL")
            if ok != "OK" or not data or not data[0]:
                continue
            for num in data[0].split():
                ok, parts = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                if ok != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                header = email.message_from_bytes(parts[0][1])
                addr = email.utils.parseaddr(header.get("From", ""))[1].strip().lower()
                if not addr or "@" not in addr or addr == user.lower():
                    continue
                if ROBOT_RE.match(addr.split("@", 1)[0]):
                    continue
                subject = decode_subject(header.get("Subject", ""))
                action = classify(subject)
                if action is None:
                    continue
                try:
                    when = email.utils.parsedate_to_datetime(header.get("Date", ""))
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                except Exception:
                    when = epoch
                prev = state.get(addr)
                if prev is None or when >= prev[0]:
                    state[addr] = (when, action, pick_lang(subject))
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return {addr: lang for addr, (_, action, lang) in state.items() if action == "sub"}


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def send_all(user: str, password: str, recipients: dict[str, str],
             old: float, new: float, eff: date) -> int:
    """Send one mail per recipient; returns the number of failures."""
    ctx = ssl.create_default_context()
    smtp = smtplib.SMTP_SSL(SMTP_HOST, 465, context=ctx)
    smtp.login(user, password)
    failures = 0
    try:
        for i, (addr, lang) in enumerate(sorted(recipients.items())):
            msg = build_mail(lang, user, addr, old, new, eff)
            try:
                smtp.send_message(msg)
            except smtplib.SMTPException:
                # one reconnect attempt, then count the failure and move on
                try:
                    smtp.quit()
                except Exception:
                    pass
                try:
                    smtp = smtplib.SMTP_SSL(SMTP_HOST, 465, context=ctx)
                    smtp.login(user, password)
                    smtp.send_message(msg)
                except Exception:
                    failures += 1
            if i + 1 < len(recipients):
                time.sleep(SEND_DELAY_S)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
    return failures


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def get_credentials() -> tuple[str, str]:
    user = os.environ.get("MAIL_USER", "").strip()
    password = os.environ.get("MAIL_PASSWORD", "")
    if not user or not password:
        fail("MAIL_USER / MAIL_PASSWORD not set (repo secrets missing?)")
    return user, password


def newest_step(series) -> tuple[float, float, date]:
    (_, old), (eff, new) = series[-2], series[-1]
    return old, new, eff


def last_decrease(series) -> tuple[float, float, date]:
    for (_, a), (eff, b) in zip(reversed(series[:-1]), reversed(series[1:])):
        if b < a:
            return a, b, eff
    fail("no decrease found in the whole series")


def selftest() -> None:
    assert classify("ABO Zinscheck [DE]") == "sub"
    assert classify("Re: abo zinscheck") == "sub"
    assert classify("STOP") == "unsub"
    assert classify("Bitte ABMELDEN") == "unsub"
    assert classify("ABO — ah nein, STOP") == "unsub"      # stop wins
    assert classify("Frage zur Miete") is None
    assert classify("Abonnement kündigen") is None          # no bare-word match
    assert pick_lang("ABO Zinscheck [FR]") == "fr"
    assert pick_lang("abo [it]") == "it"
    assert pick_lang("ABO") == "de"
    assert ROBOT_RE.match("mailer-daemon")
    assert ROBOT_RE.match("noreply")
    assert not ROBOT_RE.match("hans.muster")
    # every template must render with realistic values and contain the numbers
    for lang in TEMPLATES:
        msg = build_mail(lang, "info@zinscheck.ch", "x@example.org",
                         1.50, 1.25, date(2026, 9, 2))
        body = msg.get_content()
        assert "1.50" in body and "1.25" in body and "2.91" in body, lang
        assert SITE in body and "STOP" in body, lang
        assert "1.25" in msg["Subject"], lang
    # sanity on the engine convention: 1.75 -> 1.25 must be -5.66 %
    assert abs(abs(reference_rate_change_pct(1.75, 1.25)) * 100 - 5.66) < 0.01
    # the real series must load, be chronological, and contain a decrease
    series = load_series()
    assert len(series) >= 2, "series too short"
    assert all(a[0] < b[0] for a, b in zip(series, series[1:])), "series not chronological"
    old, new, eff = last_decrease(series)
    assert new < old and isinstance(eff, date)
    print(f"selftest OK — parsing, all four templates, series ({len(series)} entries, "
          f"last decrease {old} -> {new} on {eff})")


def main() -> None:
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
        return

    series = load_series()

    if "--list" in args:
        user, password = get_credentials()
        subs = collect_subscribers(user, password)
        counts: dict[str, int] = {}
        for lang in subs.values():
            counts[lang] = counts.get(lang, 0) + 1
        by_lang = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "—"
        print(f"subscribers: {len(subs)} ({by_lang})")
        return

    if "--test" in args:
        addr = args[args.index("--test") + 1]
        user, password = get_credentials()
        old, new, eff = last_decrease(series)
        print(f"test send to 1 address, all 4 languages, using {old} -> {new} ({eff})")
        failures = 0
        for lang in TEMPLATES:
            failures += send_all(user, password, {addr: lang}, old, new, eff)
        if failures:
            fail(f"{failures} test mail(s) failed to send")
        print("test mails sent")
        return

    if "--send" in args:
        old, new, eff = newest_step(series)
        if new >= old:
            fail(f"newest step {old} -> {new} is not a decrease — refusing to send")
        if date.today() - eff > timedelta(days=FRESH_DAYS):
            fail(f"decrease took effect {eff}, older than {FRESH_DAYS} days — refusing "
                 "to send (stale re-run?)")
        user, password = get_credentials()
        subs = collect_subscribers(user, password)
        print(f"rate {fmt_rate(old)} -> {fmt_rate(new)} effective {eff}; "
              f"{len(subs)} subscriber(s)")
        if not subs:
            print("nothing to send")
            return
        failures = send_all(user, password, subs, old, new, eff)
        sent = len(subs) - failures
        print(f"sent {sent}/{len(subs)}")
        if failures:
            fail(f"{failures} mail(s) could not be sent")
        return

    print(__doc__)
    sys.exit(2)


if __name__ == "__main__":
    main()

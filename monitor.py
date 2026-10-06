#!/usr/bin/env python3
"""
Socius Connect kamer-monitor.

Logt in op connect.sociuswonen.nl, leest de aanbodpagina en stuurt een melding
zodra er een kamer, hospiteeravond of infoavond beschikbaar komt waarvoor je je
kunt aanmelden. Bedoeld om elke paar minuten te draaien via GitHub Actions.

Instellingen komen uit omgevingsvariabelen (GitHub Secrets):
  SOCIUS_USERNAME   verplicht  - je gebruikersnaam/e-mail voor Socius Connect
  SOCIUS_PASSWORD   verplicht  - je wachtwoord
  NTFY_TOPIC        optioneel  - naam van je ntfy-topic (pushmelding op telefoon)
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   optioneel - Telegram-melding
  SMTP_USER + SMTP_PASSWORD + MAIL_TO      optioneel - e-mail via Gmail
  (SMTP_HOST / SMTP_PORT optioneel, standaard smtp.gmail.com:465)

Voor lokaal testen:  python monitor.py --test-file pagina.html
Testmelding sturen:  python monitor.py --test-notify
"""

import argparse
import hashlib
import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://connect.sociuswonen.nl"
LOGIN_PAGE = BASE + "/"
LOGIN_POST = BASE + "/account/do_login"
AANBOD_URL = BASE + "/kamer_vinden/aanbod/overzicht/"
STATE_FILE = Path(__file__).with_name("state.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

NOT_POSSIBLE = "niet mogelijk om je aan te melden"


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


# --------------------------------------------------------------------------- #
# Ophalen
# --------------------------------------------------------------------------- #
def fetch_aanbod(username, password):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "nl-NL,nl;q=0.9"})

    s.get(LOGIN_PAGE, timeout=30)  # cookies ophalen
    r = s.post(
        LOGIN_POST,
        data={"email": username, "password": password,
              "submit_login": "Inloggen"},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": LOGIN_PAGE},
        timeout=30,
    )
    r.raise_for_status()

    page = s.get(AANBOD_URL, timeout=30)
    page.raise_for_status()
    html = page.text

    if 'name="password"' in html or "account/logout" not in html:
        snippet = clean(r.text)[:200]
        raise LoginError(f"Inloggen mislukt (antwoord server: {snippet!r})")
    return html


class LoginError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Uitlezen
# --------------------------------------------------------------------------- #
def parse(html):
    """Geeft (projects, available) terug.

    projects:  {projectnaam: bool  (True = aanmelden is mogelijk)}
    available: lijst met dicts voor elke regel waarop je kunt aanmelden
    """
    soup = BeautifulSoup(html, "html.parser")
    projects = {}
    available = []

    for panel in soup.select("div.panel-body"):
        h2 = panel.find("h2")
        if not h2:
            continue
        name = clean(h2.get_text())
        panel_text = clean(panel.get_text(" "))
        # "Open" = de standaardtekst 'niet mogelijk' staat er niet én er is geen
        # kamertabel (die tabel controleren we hieronder per regel). Dit vangt
        # bijv. een nieuwe hospiteer- of infoavond in een ander formaat op.
        projects[name] = (NOT_POSSIBLE not in panel_text
                          and panel.find("table") is None)

        for table in panel.find_all("table"):
            heading = table.find_previous("h4")
            section = clean(heading.get_text()) if heading else ""
            headers = [clean(th.get_text()) for th in table.find_all("th")]

            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if not tds:
                    continue
                values = [clean(td.get_text(" ")) for td in tds]
                action = tds[-1].find(["a", "button", "input"])
                action_text = clean(action.get_text()) if action else values[-1]
                classes = action.get("class", []) if action else []
                disabled = (
                    action is None
                    or "disabled" in classes
                    or action.has_attr("disabled")
                    or "geclaimed" in action_text.lower()
                    or "vol" == action_text.lower()
                )
                if disabled:
                    continue
                info = dict(zip(headers, values))
                available.append({
                    "project": name,
                    "section": section,
                    "info": info,
                    "action": action_text,
                    "key": hashlib.sha1(
                        (name + "|" + section + "|" + "|".join(values[:-1]))
                        .encode()).hexdigest()[:16],
                })
    return projects, available


def describe(item):
    i = item["info"]
    parts = [item["project"]]
    if i.get("Kamernummer"):
        parts.append(f"kamer {i['Kamernummer']}")
    if i.get("m2"):
        parts.append(f"{i['m2']} m²")
    if i.get("Totale huur"):
        parts.append(i["Totale huur"])
    if i.get("Ingangsdatum"):
        parts.append(f"per {i['Ingangsdatum']}")
    if len(parts) == 1:  # onbekende tabel: toon gewoon de cellen
        parts.append(", ".join(v for v in i.values() if v)[:150])
    line = " · ".join(parts)
    if item["section"]:
        line += f"  [{item['section']}]"
    return line


# --------------------------------------------------------------------------- #
# Meldingen
# --------------------------------------------------------------------------- #
def notify(title, message, priority="high"):
    sent = False
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": priority,
                "Tags": "house",
                "Click": AANBOD_URL,
            },
            timeout=30,
        ).raise_for_status()
        sent = True

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat,
                  "text": f"{title}\n\n{message}\n\n{AANBOD_URL}"},
            timeout=30,
        ).raise_for_status()
        sent = True

    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASSWORD")
    to = os.environ.get("MAIL_TO")
    if user and pw and to:
        msg = MIMEText(f"{message}\n\n{AANBOD_URL}", "plain", "utf-8")
        msg["Subject"] = title
        msg["From"] = user
        msg["To"] = to
        host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        port = int(os.environ.get("SMTP_PORT", "465"))
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(user, pw)
            smtp.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
        sent = True

    if not sent:
        print("LET OP: geen meldingskanaal ingesteld (NTFY_TOPIC, Telegram of SMTP).")
    print(f"[melding] {title}\n{message}")


# --------------------------------------------------------------------------- #
# Hoofdprogramma
# --------------------------------------------------------------------------- #
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"notified": [], "open_projects": [], "login_error": False}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-file", help="lees een opgeslagen HTML-pagina i.p.v. inloggen")
    ap.add_argument("--test-notify", action="store_true", help="stuur alleen een testmelding")
    args = ap.parse_args()

    if args.test_notify:
        notify("Socius-monitor test", "Als je dit ziet, werken de meldingen. 🎉")
        return

    state = load_state()

    if args.test_file:
        html = Path(args.test_file).read_text(encoding="utf-8", errors="ignore")
    else:
        user = os.environ.get("SOCIUS_USERNAME")
        pw = os.environ.get("SOCIUS_PASSWORD")
        if not user or not pw:
            sys.exit("SOCIUS_USERNAME en SOCIUS_PASSWORD ontbreken.")
        try:
            html = fetch_aanbod(user, pw)
        except LoginError as e:
            print(e)
            if not state.get("login_error"):
                notify("Socius-monitor: inloggen mislukt",
                       "De monitor kan niet inloggen. Klopt je wachtwoord nog? "
                       "Je krijgt deze melding één keer.", priority="default")
                state["login_error"] = True
                save_state(state)
            sys.exit(1)
        if state.get("login_error"):
            state["login_error"] = False

    projects, available = parse(html)
    if not projects:
        print("Waarschuwing: geen projecten gevonden; is de pagina veranderd?")

    print(f"{len(projects)} panden gecontroleerd, {len(available)} regels beschikbaar.")
    for name, open_ in projects.items():
        print(f"  - {name}: {'aanmelden mogelijk' if open_ else 'gesloten'}")

    # 1) Nieuwe beschikbare regels (kamers / hospiteeravonden / infoavonden)
    notified = set(state.get("notified", []))
    current_keys = {a["key"] for a in available}
    new = [a for a in available if a["key"] not in notified]

    # 2) Panden die net open zijn gegaan zonder dat er (al) een regel bij staat
    prev_open = set(state.get("open_projects", []))
    now_open = {n for n, o in projects.items() if o}
    has_rows = {a["project"] for a in available}
    newly_open = sorted(now_open - prev_open - has_rows)

    if new:
        lines = [describe(a) for a in new]
        title = (f"🏠 {len(new)} nieuwe kamer(s) bij Socius!"
                 if len(new) > 1 else "🏠 Nieuwe kamer bij Socius!")
        notify(title, "\n".join(lines) + "\n\nSnel reageren!", priority="urgent")

    if newly_open:
        notify("🏠 Socius: aanmelden weer mogelijk",
               "Bij deze panden kun je je nu aanmelden:\n" + "\n".join(newly_open),
               priority="high")

    # Onthoud alleen wat nu nog beschikbaar is, zodat een kamer die terugkomt
    # opnieuw een melding geeft.
    state["notified"] = sorted(current_keys)
    state["open_projects"] = sorted(now_open)
    if not args.test_file:
        save_state(state)


if __name__ == "__main__":
    main()

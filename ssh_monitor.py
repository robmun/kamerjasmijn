#!/usr/bin/env python3
"""
SSH woning-monitor (sshxl.nl).

De SSH-site is een JavaScript-app: een gewone download geeft een lege pagina.
Daarom opent dit script de site in een echte (onzichtbare) Chrome-browser via
Playwright, logt in als dat nodig is, en leest het aanbod op twee manieren:

  1. de gegevens die de app zelf ophaalt (JSON-antwoorden van de server);
  2. de links naar advertenties op de zichtbare pagina (reserve).

Nieuw aanbod ten opzichte van ssh_state.json -> één e-mail aan MAIL_TO_SSH
(standaard Robert en Jasmijn) en, als NTFY_TOPIC is ingesteld, een pushmelding.

Draait via GitHub Actions rond 09:00 en 13:00 (Amsterdamse tijd).

Instellingen (GitHub Secrets):
  SSH_USERNAME, SSH_PASSWORD   optioneel - inloggegevens voor sshxl.nl
  SMTP_USER, SMTP_PASSWORD     verplicht voor e-mail (Gmail + app-wachtwoord)
  MAIL_TO_SSH                  optioneel - ontvangers, komma-gescheiden
  NTFY_TOPIC                   optioneel - pushmelding

Lokaal:
  python ssh_monitor.py --force         controle nu uitvoeren (tijdvenster negeren)
  python ssh_monitor.py --test-notify   alleen een testmail sturen
  python ssh_monitor.py --test-json f   parser testen op een opgeslagen JSON-bestand
"""

import argparse
import hashlib
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

BASE = os.environ.get("SSH_BASE", "https://www.sshxl.nl")  # SSH_BASE alleen voor testen
# Pagina's waar aanbod kan staan. De eerste die aanbod oplevert wint.
OFFER_PAGES = [
    BASE + "/nl/aanbod",
    BASE + "/aanbod",
    BASE + "/en/rental-offer",
    BASE + "/en/offer",
    BASE + "/nl",
    BASE + "/",
]
OFFER_LINK_WORDS = ("aanbod", "huuraanbod", "rental offer", "offer", "woningaanbod", "kamers")
DETAIL_HREF = re.compile(r"/(aanbod|offer|rental-offer|woning|accommodation|unit|object|property|detail)s?/[^/?#]+", re.I)

STATE_FILE = Path(__file__).with_name("ssh_state.json")
DEBUG_DIR = Path(__file__).with_name("ssh_debug")
TZ = ZoneInfo("Europe/Amsterdam")
DEFAULT_TO = "robertmunnichs@gmail.com,jasmijnmunnichs@gmail.com"

# Velden die in JSON-aanbod vaak voorkomen (lowercase, zonder _ en -)
ID_KEYS = ("id", "uuid", "objectid", "unitid", "offerid", "advertid", "code", "slug", "reference")
NAME_KEYS = ("name", "title", "naam", "titel", "address", "adres", "street", "straat",
             "streetname", "complex", "complexname", "building", "description")
PRICE_KEYS = ("rent", "huur", "price", "prijs", "totalrent", "netrent", "basicrent", "kalehuur", "rentprice")
EXTRA_KEYS = {
    "plaats": ("city", "plaats", "woonplaats", "town", "location", "locatie"),
    "type": ("type", "objecttype", "unittype", "category", "soort", "dwellingtype"),
    "m2": ("surface", "area", "size", "m2", "oppervlakte", "livingarea"),
    "beschikbaar": ("availablefrom", "available", "beschikbaarper", "startdate", "ingangsdatum", "availabilitydate"),
    "deadline": ("deadline", "enddate", "closingdate", "reactiondeadline", "einddatum", "publicationend", "sluitingsdatum"),
    "voorwaarden": ("conditions", "voorwaarden", "requirements", "targetgroup", "doelgroep", "eligibility"),
    "toewijzing": ("allocation", "toewijzing", "allocationtype", "model", "lottery", "loting"),
}
URL_KEYS = ("url", "link", "href", "permalink", "detailurl", "path")


def norm(k):
    return re.sub(r"[_\-\s]", "", str(k).lower())


def clean(t):
    return re.sub(r"\s+", " ", str(t or "")).strip()


# --------------------------------------------------------------------------- #
# JSON-aanbod herkennen
# --------------------------------------------------------------------------- #
def _pick(d, keys):
    nd = {norm(k): v for k, v in d.items()}
    for k in keys:
        v = nd.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _scalar(v):
    if isinstance(v, dict):
        for k in ("name", "title", "label", "value", "amount", "nl", "en"):
            if k in v and not isinstance(v[k], (dict, list)):
                return v[k]
        return None
    if isinstance(v, list):
        parts = [_scalar(x) for x in v[:5]]
        return ", ".join(str(p) for p in parts if p not in (None, ""))
    return v


def looks_like_offer(d):
    if not isinstance(d, dict):
        return False
    keys = {norm(k) for k in d}
    has_id = any(k in keys for k in ID_KEYS)
    has_name = any(k in keys for k in NAME_KEYS)
    has_price = any(k in keys for k in PRICE_KEYS)
    has_housing = any(k in keys for k in EXTRA_KEYS["m2"] + EXTRA_KEYS["beschikbaar"])
    # Huur of woninggegevens zijn verplicht, zodat nieuwsberichten e.d. niet meetellen.
    return has_id and (has_name or has_price) and (has_price or has_housing)


def find_offer_lists(obj, path="$"):
    """Geef alle lijsten terug die op aanbod lijken: [(pad, [dict, ...])]."""
    found = []
    if isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if dicts and sum(looks_like_offer(x) for x in dicts) >= max(1, len(dicts) // 2):
            found.append((path, dicts))
        for i, x in enumerate(obj[:200]):
            found += find_offer_lists(x, f"{path}[{i}]")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            found += find_offer_lists(v, f"{path}.{k}")
    return found


def offer_from_json(d, source_url):
    oid = _scalar(_pick(d, ID_KEYS))
    name = clean(_scalar(_pick(d, NAME_KEYS)))
    price = _scalar(_pick(d, PRICE_KEYS))
    url = _scalar(_pick(d, URL_KEYS))
    if url and isinstance(url, str):
        url = urljoin(BASE, url)
    item = {
        "key": f"json:{oid}" if oid not in (None, "") else "json:" + hashlib.sha1(
            json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:16],
        "naam": name[:160] or f"Aanbod {oid}",
        "huur": clean(price) if price not in (None, "") else "",
        "url": url or "",
        "bron": source_url,
    }
    for label, keys in EXTRA_KEYS.items():
        v = _scalar(_pick(d, keys))
        if v not in (None, ""):
            item[label] = clean(v)[:200]
    return item


def parse_card(txt):
    """Haal adres, huur en details uit de tekst van een SSH-aanbodkaart."""
    it = {"naam": txt[:200]}
    m = re.match(r"(.+?),\s*([A-Z][\w' -]+?)\s+€", txt)
    if m:
        it["naam"], it["plaats"] = m.group(1).strip(), m.group(2).strip()
    pats = {
        "huur": r"€\s*([\d.]+,\d{2})\s*/\s*mnd",
        "beschikbaar": r"start op\s*(\d{2}-\d{2}-\d{4})",
        "m2": r"Oppervlakte:\s*([\d.,]+)\s*m",
        "type": r"Type woning:\s*(.+?)(?=\s+(?:Manier|Reageren|Reageer|Toewijzing|Beschikbaar|Huur|Oppervlakte|Type|Deadline|Bekijk)|$)",
        "deadline": r"(?:Reageren tot|Reageer voor|Deadline|Sluit(?:ingsdatum)?)[:\s]*([\d-]{8,10}[^A-Z]*)",
        "toewijzing": r"Manier van toewijz\w*:\s*(.+?)(?=\s+(?:Reageren|Reageer|Huur|Oppervlakte|Type|Deadline|Bekijk)|$)|\b(Loting|Inschrijfduur|Wie het eerst komt|Direct huren|Voorrang[^.]*)",
    }
    for k, p in pats.items():
        m = re.search(p, txt, re.I)
        if m:
            it[k] = clean(next(g for g in m.groups() if g))[:120]
    if it.get("huur"):
        it["huur"] = "€ " + it["huur"]
    m = re.search(r"Huur bij max\.?huurtoeslag:\s*€\s*([\d.]+,\d{2})", txt)
    if m:
        it["voorwaarden"] = f"na max. huurtoeslag € {m.group(1)}"
    return it


def wanted(it, cities):
    plaats = (it.get("plaats") or "").lower()
    if not cities:
        return True
    if not plaats:  # plaats onbekend: liever melden dan missen
        return True
    return any(c in plaats for c in cities)


# --------------------------------------------------------------------------- #
# Browser
# --------------------------------------------------------------------------- #
def try_login(page, user, pw):
    """Algemene inlogpoging: zoek een loginknop, vul e-mail en wachtwoord in."""
    if not (user and pw):
        return "geen inloggegevens"
    if page.locator("input[type=password]").count() == 0:
        for label in ("Inloggen", "Log in", "Login", "Mijn SSH", "My SSH", "Aanmelden"):
            loc = page.get_by_role("link", name=re.compile(label, re.I))
            if loc.count() == 0:
                loc = page.get_by_role("button", name=re.compile(label, re.I))
            if loc.count():
                try:
                    loc.first.click(timeout=5000)
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                if page.locator("input[type=password]").count():
                    break
    if page.locator("input[type=password]").count() == 0:
        return "geen inlogformulier gevonden"
    user_box = page.locator(
        "input[type=email], input[name*=user i], input[name*=mail i], input[id*=user i], "
        "input[id*=mail i], input[autocomplete=username]").first
    try:
        user_box.fill(user, timeout=5000)
        page.locator("input[type=password]").first.fill(pw, timeout=5000)
        page.locator("input[type=password]").first.press("Enter")
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception as e:
        return f"inloggen mislukt: {e.__class__.__name__}"
    if page.locator("input[type=password]").count():
        return "inloggen mislukt (formulier staat er nog)"
    return "ingelogd"


def scrape(user, pw, debug=True):
    from playwright.sync_api import sync_playwright

    json_hits = []      # (url, data)
    log = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("CHROME_PATH") or None)
        ctx = browser.new_context(locale="nl-NL", timezone_id="Europe/Amsterdam",
                                  viewport={"width": 1280, "height": 1800})
        page = ctx.new_page()

        def on_response(resp):
            try:
                host = urlparse(resp.url).hostname or ""
                if "sshxl" not in host and host != urlparse(BASE).hostname:
                    return
                if "json" not in (resp.headers.get("content-type") or ""):
                    return
                json_hits.append((resp.url, resp.json()))
            except Exception:
                pass

        page.on("response", on_response)

        page.goto(BASE + "/", wait_until="networkidle", timeout=60000)
        log.append("login: " + try_login(page, user, pw))

        # Zoek een link naar het aanbod op de site zelf
        pages = list(OFFER_PAGES)
        for a in page.locator("a[href]").all()[:300]:
            try:
                txt = clean(a.inner_text(timeout=1000)).lower()
                href = a.get_attribute("href") or ""
            except Exception:
                continue
            if any(w in txt for w in OFFER_LINK_WORDS) and href and not href.startswith(("mailto:", "tel:")):
                pages.insert(0, urljoin(page.url, href))
        seen_pages = []
        dom_items = {}
        for url in dict.fromkeys(u.rstrip("/") or u for u in pages):
            if len(seen_pages) >= 4:
                break
            try:
                page.goto(url, wait_until="networkidle", timeout=45000)
                page.wait_for_timeout(2500)
                # scroll om lazy-loaded kaarten te laden
                for _ in range(6):
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(500)
                # knoppen als "Toon meer" / "Meer laden" doorklikken
                for _ in range(15):
                    more = page.get_by_role("button", name=re.compile(r"(toon|laad|meer|more|load)", re.I))
                    if more.count() == 0 or not more.first.is_visible():
                        break
                    more.first.click(timeout=5000)
                    page.wait_for_load_state("networkidle", timeout=20000)
                    page.wait_for_timeout(800)
            except Exception as e:
                log.append(f"{url}: {e.__class__.__name__}")
                continue
            seen_pages.append(page.url)
            for a in page.locator("a[href]").all()[:600]:
                try:
                    href = urljoin(page.url, a.get_attribute("href") or "")
                    h = urlparse(href).hostname
                    if h and "sshxl" not in h and h != urlparse(BASE).hostname:
                        continue
                    if not DETAIL_HREF.search(urlparse(href).path):
                        continue
                    card = a.locator("xpath=ancestor-or-self::*[self::article or self::li or contains(@class,'card')][1]")
                    txt = clean((card.first if card.count() else a).inner_text(timeout=1000))
                except Exception:
                    continue
                if len(txt) < 8 or href.rstrip("/") in (u.rstrip("/") for u in OFFER_PAGES):
                    continue
                key = "url:" + href.split("#")[0].rstrip("/")
                dom_items.setdefault(key, {"key": key, **parse_card(txt), "url": href, "bron": page.url})
            if debug:
                DEBUG_DIR.mkdir(exist_ok=True)
                slug = re.sub(r"[^a-z0-9]+", "-", urlparse(page.url).path.lower()).strip("-") or "home"
                page.screenshot(path=str(DEBUG_DIR / f"{slug}.png"), full_page=True)
                (DEBUG_DIR / f"{slug}.html").write_text(page.content(), encoding="utf-8")
        browser.close()

    json_items = {}
    for url, data in json_hits:
        for path, lst in find_offer_lists(data):
            for d in lst:
                it = offer_from_json(d, url)
                json_items.setdefault(it["key"], it)
    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / "json_urls.txt").write_text(
            "\n".join(f"{u}  ({len(json.dumps(d, default=str))} bytes)" for u, d in json_hits), encoding="utf-8")
        for i, (u, d) in enumerate(json_hits[:40]):
            (DEBUG_DIR / f"json_{i:02d}.json").write_text(json.dumps(d, indent=1, default=str)[:400000], encoding="utf-8")

    items = json_items if json_items else dom_items
    method = "json" if json_items else ("dom" if dom_items else "geen")
    log.append(f"pagina's: {', '.join(seen_pages) or '-'}")
    log.append(f"json-antwoorden: {len(json_hits)}, aanbod via json: {len(json_items)}, via links: {len(dom_items)}")
    return list(items.values()), method, log


# --------------------------------------------------------------------------- #
# Meldingen
# --------------------------------------------------------------------------- #
def send_mail(subject, text, html):
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    to = [x.strip() for x in (os.environ.get("MAIL_TO_SSH") or DEFAULT_TO).split(",") if x.strip()]
    if not (user and pw):
        print("LET OP: SMTP_USER/SMTP_PASSWORD ontbreken, geen e-mail verstuurd.")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(to)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    for attempt in (1, 2):
        try:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(user, pw)
                s.sendmail(user, to, msg.as_string())
            print(f"E-mail verstuurd aan {', '.join(to)}")
            return True
        except Exception as e:
            print(f"E-mail mislukt (poging {attempt}): {e}")
    return False


def push(title, message):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    try:
        import requests
        requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                      headers={"Title": title.encode("utf-8"), "Priority": "high", "Tags": "house"},
                      timeout=20)
    except Exception as e:
        print(f"ntfy mislukt: {e}")


def telegram(html_text):
    """Stuur een bericht via de Telegram-bot (zelfde bot als de Socius-monitor)."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    import requests
    # Telegram staat max. 4096 tekens per bericht toe: in stukken knippen op lege regels.
    chunks, cur = [], ""
    for block in html_text.split("\n\n"):
        if len(cur) + len(block) + 2 > 3800 and cur:
            chunks.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + block
    chunks.append(cur)
    ok = True
    for c in chunks:
        for attempt in (1, 2):
            try:
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                  data={"chat_id": chat, "text": c, "parse_mode": "HTML",
                                        "disable_web_page_preview": "true"}, timeout=30)
                r.raise_for_status()
                break
            except Exception as e:
                print(f"Telegram mislukt (poging {attempt}): {e.__class__.__name__}")
                if attempt == 2:
                    ok = False
    if ok:
        print("Telegram-bericht verstuurd.")
    return ok


def describe_tg(it):
    lines = [f"<b>{escape(it['naam'])}</b>"]
    for k in ("huur", "type", "m2", "beschikbaar", "deadline", "toewijzing", "voorwaarden"):
        if it.get(k):
            v = f"{it[k]} m²" if k == "m2" else it[k]
            label = {"m2": "oppervlakte", "beschikbaar": "start", "deadline": "reageren tot"}.get(k, k)
            lines.append(f"{label}: {escape(str(v))}")
    link = it.get("url") or it.get("bron") or BASE
    lines.append(f'<a href="{escape(link)}">Bekijk op SSH</a>')
    return "\n".join(lines)


def describe_text(it):
    parts = [it["naam"]]
    for k in ("plaats", "type", "huur", "m2", "beschikbaar", "deadline", "toewijzing", "voorwaarden"):
        if it.get(k):
            parts.append(f"{k}: {it[k]}")
    parts.append(it.get("url") or it.get("bron") or BASE)
    return "\n  ".join(parts)


def describe_html(it):
    rows = "".join(f"<br><span style='color:#5f6b67'>{escape(k)}:</span> {escape(str(it[k]))}"
                   for k in ("plaats", "type", "huur", "m2", "beschikbaar", "deadline", "toewijzing", "voorwaarden")
                   if it.get(k))
    link = it.get("url") or it.get("bron") or BASE
    return (f"<li style='margin-bottom:10px'><b>{escape(it['naam'])}</b>{rows}"
            f"<br><a href='{escape(link)}'>Bekijk op SSH</a></li>")


def alert(new_items, today):
    n = len(new_items)
    subject = f"[Utrecht Housing Alert] {n} nieuw SSH-aanbod — {today}"
    text = (f"Nieuw bij SSH ({n}):\n\n" + "\n\n".join("- " + describe_text(i) for i in new_items)
            + "\n\nControleer de voorwaarden en deadline op de site.")
    html = (f"<div style='font-family:Arial,sans-serif;font-size:14px;line-height:1.5'>"
            f"<p>Nieuw bij SSH ({n}):</p><ol style='padding-left:18px'>"
            + "".join(describe_html(i) for i in new_items)
            + "</ol><p style='color:#5f6b67'>Controleer de voorwaarden, deadline en loting op de site.</p></div>")
    ok_tg = telegram(f"🏠 <b>{n} nieuw SSH-aanbod in Utrecht</b>\n\n"
                     + "\n\n".join(describe_tg(i) for i in new_items))
    ok_mail = send_mail(subject, text, html) if os.environ.get("SMTP_USER") else False
    push(f"🏠 {n} nieuw SSH-aanbod", "\n".join(i["naam"][:80] for i in new_items[:8]))
    return ok_tg or ok_mail


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def current_slot(now):
    if now.hour in (8, 9):
        return "ochtend"
    if now.hour in (12, 13):
        return "middag"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="tijdvenster negeren")
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("--test-json")
    args = ap.parse_args()
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")

    if args.test_notify:
        ok = telegram("✅ Testbericht van de SSH-monitor. Als je dit leest, werkt Telegram.")
        if os.environ.get("SMTP_USER"):
            ok = send_mail(f"[Utrecht Housing Alert] Testmail SSH-monitor — {today}",
                       "Dit is een testmail van de SSH-monitor. Als je dit leest, werkt de e-mail.",
                       "<p>Dit is een testmail van de SSH-monitor. Als je dit leest, werkt de e-mail.</p>") or ok
        push("SSH-monitor test", "Testmelding van de SSH-monitor.")
        sys.exit(0 if ok else 1)

    if args.test_json:
        data = json.loads(Path(args.test_json).read_text(encoding="utf-8"))
        for path, lst in find_offer_lists(data):
            print(path, len(lst))
            for d in lst:
                print("  ", describe_text(offer_from_json(d, "test")).replace("\n", " | "))
        return

    state = load_state()
    slot = current_slot(now)
    if not args.force:
        if slot is None:
            print(f"{now:%H:%M}: buiten het tijdvenster (09:00/13:00), niets te doen.")
            return
        if state.get("last_slot") == f"{today}-{slot}":
            print(f"{now:%H:%M}: {slot}-controle van vandaag is al gedaan.")
            return

    try:
        items, method, log = scrape(os.environ.get("SSH_USERNAME"), os.environ.get("SSH_PASSWORD"))
    except Exception as e:
        items, method, log = [], "fout", [f"fout: {e!r}"]
    for line in log:
        print(line)
    print(f"{len(items)} aanbiedingen gevonden (methode: {method})")

    run = {"at": now.isoformat(timespec="minutes"), "found": len(items), "method": method, "log": log}
    state.setdefault("runs", [])
    state["runs"] = ([run] + state["runs"])[:60]
    if slot and not args.force:
        state["last_slot"] = f"{today}-{slot}"

    if not items:
        # Mislukte controle is NIET hetzelfde als 'niets nieuw': niets wissen, één keer waarschuwen.
        state["fail_count"] = state.get("fail_count", 0) + 1
        if state["fail_count"] == 3:
            telegram("⚠️ De SSH-monitor heeft drie keer achter elkaar geen aanbod kunnen lezen. "
                     "Kijk in GitHub bij Actions naar de laatste run (bestand ssh-debug).")
            if os.environ.get("SMTP_USER"):
                send_mail(f"[Utrecht Housing Alert] SSH-monitor leest geen aanbod meer — {today}",
                      "De SSH-monitor heeft drie keer achter elkaar geen aanbod kunnen lezen. "
                      "Kijk in GitHub bij Actions naar de laatste run (bestand ssh-debug).",
                      "<p>De SSH-monitor heeft drie keer achter elkaar geen aanbod kunnen lezen. "
                      "Kijk in GitHub bij <b>Actions</b> naar de laatste run (bestand <i>ssh-debug</i>).</p>")
        save_state(state)
        sys.exit(1)
    state["fail_count"] = 0

    cities = [c.strip().lower() for c in (os.environ.get("SSH_CITIES") or "Utrecht").split(",") if c.strip()]
    items = [i for i in items if wanted(i, cities)]
    print(f"{len(items)} in {', '.join(cities) or 'alle plaatsen'}")
    known = {k: v for k, v in state.get("items", {}).items() if wanted(v, cities)}
    first_run = not known
    new = [i for i in items if i["key"] not in known]
    pending = [k for k, v in known.items() if v.get("pending_mail")]
    for it in items:
        rec = known.get(it["key"], {"first_seen": today})
        rec.update({"naam": it["naam"], "plaats": it.get("plaats", ""), "url": it.get("url", ""),
                    "huur": it.get("huur", ""), "last_seen": today})
        known[it["key"]] = rec
    to_mail = [] if first_run else new + [i for i in items if i["key"] in pending and i not in new]

    if first_run:
        print(f"Eerste run: {len(items)} aanbiedingen als nulmeting opgeslagen, geen melding.")
    elif to_mail:
        ok = alert(to_mail, today)
        for it in to_mail:
            known[it["key"]]["pending_mail"] = not ok
            if ok:
                known[it["key"]]["notified"] = today
    else:
        print("Niets nieuw.")
    state["items"] = known
    save_state(state)


if __name__ == "__main__":
    main()

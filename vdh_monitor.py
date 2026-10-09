#!/usr/bin/env python3
"""
Van der Huizen woning-monitor (Utrecht).

Leest https://www.vanderhuizen.com/provincies/utrecht/utrecht-1, vergelijkt met
vdh_state.json en stuurt via Telegram (zelfde bot als de andere monitors) een
bericht bij nieuwe woningen of een gewijzigde huurprijs, met per woning de link.

Draait in dezelfde GitHub-workflow als de SSH-monitor, rond 09:00 en 13:00.

  python vdh_monitor.py --force         controle nu uitvoeren
  python vdh_monitor.py --test-file f   parser testen op een opgeslagen pagina
"""

import argparse
import json
import re
import sys
from datetime import datetime
from html import escape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from ssh_monitor import TZ, current_slot, telegram

LIST_URL = "https://www.vanderhuizen.com/provincies/utrecht/utrecht-1"
STATE_FILE = Path(__file__).with_name("vdh_state.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


def clean(t):
    return re.sub(r"\s+", " ", (t or "").replace("\xa0", " ")).strip()


def get(url):
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "nl-NL,nl;q=0.9"}, timeout=40)
    r.raise_for_status()
    return r.text


def parse_list(html):
    soup = BeautifulSoup(html, "html.parser")
    items = {}
    for art in soup.select("article.House"):
        a = art.select_one("h3 a[href]")
        if not a:
            continue
        url = a["href"].split("#")[0].split("?")[0]
        price = clean(art.select_one(".price").get_text(" ")) if art.select_one(".price") else ""
        m = re.search(r"€\s*([\d.]+,\d{2})", price)
        cap = art.select_one("figcaption")
        items[url] = {
            "url": url,
            "adres": clean(a.get_text()),
            "plaats": clean(art.select_one("h4").get_text()) if art.select_one("h4") else "",
            "huur": m.group(1) if m else "",
            "type": clean(art.select_one(".category").get_text()) if art.select_one(".category") else "",
            "label": clean(cap.get_text()) if cap else "",
        }
    # paginering: links met ?page= of /page/
    pages = {a["href"] for a in soup.select("a[href]") if re.search(r"[?&]page=\d+|/page/\d+", a["href"])}
    return items, sorted(pages)


def field(txt, label):
    m = re.search(re.escape(label) + r"\n([^\n]+)", txt)
    return clean(m.group(1)) if m else ""


def parse_detail(html):
    soup = BeautifulSoup(html, "html.parser")
    txt = (soup.find("main") or soup.body or soup).get_text("\n", strip=True).replace("\xa0", " ")
    d = {
        "huur_allin": re.sub(r"\s*per maand", "", field(txt, "Huurprijs all-in") or field(txt, "Huurprijs")),
        "m2": field(txt, "Woonoppervlakte"),
        "kamers": field(txt, "Aantal kamers"),
        "status": field(txt, "Status"),
        "beschikbaar": field(txt, "Beschikbaar vanaf"),
    }
    m = re.search(r"((?:bestemd|geschikt) voor.{3,160}?)(?:\.\s+[A-Z]|\n|,\s*conform|\.$)", txt, re.I)
    if m:
        d["doelgroep"] = clean(m.group(1))
    if re.search(r"maximaal aantal (reacties|inschrijvingen)", txt, re.I):
        d["let_op"] = "maximaal aantal reacties bereikt"
    elif re.search(r"Bezichtiging[^\n]*niet mogelijk", txt, re.I):
        d["let_op"] = "bezichtiging op dit moment niet mogelijk"
    return {k: v for k, v in d.items() if v}


def is_2_3(it):
    return bool(re.search(r"\b[23]-kamer", it.get("type", ""), re.I)) or it.get("kamers") in ("2", "3")


def describe(it, change=None):
    lines = [f"<b>{escape(it['adres'])}</b>"]
    if change:
        lines.append(f"🔁 {escape(change)}")
    huur = f"€ {it['huur']} kaal" if it.get("huur") else ""
    if it.get("huur_allin"):
        huur += f" / {it['huur_allin']} all-in"
    for label, v in (("huur", huur), ("type", it.get("type")),
                     ("oppervlakte", it.get("m2")), ("beschikbaar", it.get("beschikbaar")),
                     ("voor", it.get("doelgroep")), ("let op", it.get("let_op"))):
        if v:
            lines.append(f"{label}: {escape(v)}")
    lines.append(escape(it["url"]))
    return "\n".join(lines)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--test-file")
    args = ap.parse_args()

    if args.test_file:
        items, pages = parse_list(Path(args.test_file).read_text(encoding="utf-8"))
        for it in items.values():
            print(it)
        print("paginering:", pages)
        return

    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    state = load_state()
    slot = current_slot(now)
    if not args.force:
        if slot is None:
            print(f"{now:%H:%M}: buiten het tijdvenster (09:00/13:00), niets te doen.")
            return
        if state.get("last_slot") == f"{today}-{slot}":
            print(f"{now:%H:%M}: {slot}-controle van vandaag is al gedaan.")
            return

    # ---- ophalen (met paginering) ----
    items, error = {}, ""
    try:
        html = get(LIST_URL)
        items, pages = parse_list(html)
        for p in pages[:10]:
            more, _ = parse_list(get(requests.compat.urljoin(LIST_URL, p)))
            items.update(more)
    except Exception as e:
        error = f"ophalen mislukt: {e.__class__.__name__}"
    print(f"{len(items)} woningen gevonden. {error}")

    run = {"at": now.isoformat(timespec="minutes"), "found": len(items), "error": error}
    state["runs"] = ([run] + state.get("runs", []))[:60]
    if slot and not args.force:
        state["last_slot"] = f"{today}-{slot}"

    if not items:
        # Mislukte controle telt niet als 'niets nieuw'; niets wissen, na 3x één waarschuwing.
        state["fail_count"] = state.get("fail_count", 0) + 1
        if state["fail_count"] == 3:
            telegram("⚠️ De Van der Huizen-monitor kon drie keer achter elkaar geen woningen lezen. "
                     f"Kijk zelf even op {LIST_URL}")
        save_state(state)
        sys.exit(1)
    state["fail_count"] = 0

    known = state.setdefault("items", {})
    first_run = not known
    new, changed = [], []
    by_addr = {(v.get("adres", "").lower(), v.get("type", "").lower()): k for k, v in known.items()}

    for url, it in items.items():
        rec = known.get(url)
        if rec is None:
            # herplaatsing van dezelfde woning met een nieuwe URL?
            old_key = by_addr.get((it["adres"].lower(), it["type"].lower()))
            if old_key and old_key != url:
                rec = known.pop(old_key)
                rec["url"] = url
                known[url] = rec
                print(f"Herplaatst: {it['adres']}")
        if rec is None:
            if not first_run:
                try:
                    it.update(parse_detail(get(url)))
                except Exception as e:
                    print(f"Details {url}: {e.__class__.__name__}")
                new.append(it)
            known[url] = {**it, "first_seen": today, "last_seen": today, "missing": 0,
                          "status": "available", "pending": not first_run}
            continue
        if rec.get("huur") and it["huur"] and rec["huur"] != it["huur"]:
            changed.append((it, f"huur gewijzigd: € {rec['huur']} → € {it['huur']}"))
        if rec.get("status") == "removed":
            changed.append((it, "staat weer online"))
        rec.update({k: v for k, v in it.items() if v}, last_seen=today, missing=0, status="available")

    for url, rec in known.items():
        if url not in items and rec.get("status") != "removed":
            rec["missing"] = rec.get("missing", 0) + 1
            if rec["missing"] >= 2:
                rec["status"] = "removed"

    # eerder niet verstuurde meldingen opnieuw meenemen
    retry = [dict(known[u], **items[u]) for u, r in known.items()
             if r.get("pending") and u in items and u not in {i["url"] for i in new}]
    to_send = sorted(new + retry, key=lambda i: (not is_2_3(i), i["huur"]))
    if first_run:
        print(f"Eerste run: {len(items)} woningen als nulmeting opgeslagen, geen melding.")
    elif to_send or changed:
        parts = []
        if to_send:
            parts.append(f"🏠 <b>{len(to_send)} nieuw bij Van der Huizen (Utrecht)</b>")
            parts += [describe(i) for i in to_send]
        if changed:
            parts.append(f"🔁 <b>{len(changed)} gewijzigd bij Van der Huizen</b>")
            parts += [describe(i, c) for i, c in changed]
        ok = telegram("\n\n".join(parts))
        for i in to_send:
            known[i["url"]]["pending"] = not ok
            if ok:
                known[i["url"]]["notified"] = today
    else:
        print("Niets nieuw.")
    save_state(state)


if __name__ == "__main__":
    main()

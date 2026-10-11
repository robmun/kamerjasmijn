# Socius kamer-monitor

Controleert elke 5 minuten het aanbod op Socius Connect en stuurt een melding (ntfy,
Telegram en mail aan Robert en Jasmijn) zodra er een kamer vrijkomt waarvoor je je kunt aanmelden.

## Installatie (± 10 minuten)

### 1. Meldingen op je telefoon (ntfy)
1. Installeer de app **ntfy** (iPhone of Android).
2. Tik op **+** en abonneer je op een topic met een onvoorspelbare naam, bijv.
   `socius-jasmijn-8k3f92q` (iedereen die de naam kent kan meelezen, dus maak hem lastig te raden).
3. Zet in de app-instellingen meldingen voor dit topic op hoge prioriteit.

### 2. GitHub-repository maken
1. Maak een (gratis) account op github.com.
2. Klik **New repository** → naam bijv. `socius-monitor` → kies **Public** → **Create**.
   *Public is nodig omdat GitHub Actions dan onbeperkt gratis is. Je wachtwoord staat
   als "secret" opgeslagen en is nooit zichtbaar voor anderen.*
3. Klik **uploading an existing file** en sleep alle bestanden uit deze map erin,
   inclusief de map `.github` (op een Mac: druk Cmd+Shift+. om verborgen mappen te zien).
   Controleer na het uploaden dat het bestand `.github/workflows/monitor.yml` bestaat.

### 3. Geheimen instellen
Ga in je repository naar **Settings → Secrets and variables → Actions → New repository secret**
en voeg toe:

| Naam | Waarde |
|---|---|
| `SOCIUS_USERNAME` | je gebruikersnaam voor Socius Connect |
| `SOCIUS_PASSWORD` | je wachtwoord |
| `NTFY_TOPIC` | de topicnaam uit stap 1 |

Extra kanalen:
- **E-mail (Gmail):** `SMTP_USER` (je Gmail-adres) en `SMTP_PASSWORD` (een *app-wachtwoord*,
  aan te maken via myaccount.google.com → Beveiliging → App-wachtwoorden). De mail gaat standaard
  naar **robertmunnichs@gmail.com en jasmijnmunnichs@gmail.com**; met het secret `MAIL_TO`
  (komma-gescheiden) kies je andere ontvangers.
- **Telegram:** `TELEGRAM_BOT_TOKEN` en `TELEGRAM_CHAT_ID`.

### 4. Testen
1. Ga naar het tabblad **Actions** → **Socius kamer-monitor** → **Run workflow**.
2. Vink **Alleen een testmelding sturen** aan → je telefoon moet nu een melding krijgen.
3. Draai hem nog een keer zonder vinkje. Klik op de run → stap **Aanbod controleren**:
   je hoort te zien `3 panden gecontroleerd, ...`. Zie je "Inloggen mislukt", stuur die regel dan door.

Daarna draait hij vanzelf elke ~5 minuten.

## Wat je krijgt
- 🏠 **Nieuwe kamer** — pand, kamernummer, m², huur en ingangsdatum. Je krijgt per kamer één melding.
- 🏠 **Aanmelden weer mogelijk** — een pand waar de tekst "niet mogelijk om je aan te melden"
  verdwijnt (bijv. een nieuwe hospiteer- of infoavond).
- **Inloggen mislukt** — één keer, als je wachtwoord niet meer werkt.

## Goed om te weten
- GitHub voert "elke 5 minuten" niet altijd stipt uit; soms zit er 10–15 minuten tussen.
- GitHub zet geplande workflows uit na 60 dagen zonder activiteit in de repository.
  Je krijgt dan een mail; klik op **Enable workflow** in het tabblad Actions.
- Uitzetten: tabblad **Actions** → workflow → **⋯** → **Disable workflow**.

---

# SSH woning-monitor

`ssh_monitor.py` controleert het aanbod op sshxl.nl één keer per dagdeel (**ochtend 08–12 uur,
middag 12–23 uur**, Amsterdamse tijd) en stuurt bij nieuw aanbod een Telegram-bericht én een mail
aan Robert en Jasmijn, met klikbare links naar de advertenties. De SSH-site werkt alleen in een
browser, dus het script gebruikt een onzichtbare Chrome-browser (Playwright).

## Installeren
1. Upload `ssh_monitor.py`, `requirements-ssh.txt` en `.github/workflows/ssh-monitor.yml`.
2. Voeg onder **Settings → Secrets and variables → Actions** toe:

| Naam | Waarde |
|---|---|
| `SSH_USERNAME` | je gebruikersnaam/e-mail voor sshxl.nl |
| `SSH_PASSWORD` | je SSH-wachtwoord |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | dezelfde als voor de Socius-monitor (meldingen via Telegram) |
| `SMTP_USER` | voor e-mail: robertmunnichs@gmail.com (de afzender) |
| `SMTP_PASSWORD` | voor e-mail: een Gmail-app-wachtwoord |

De mail gaat standaard naar robertmunnichs@gmail.com en jasmijnmunnichs@gmail.com.
Optioneel: `MAIL_TO_SSH` (andere ontvangers, komma-gescheiden). `NTFY_TOPIC` wordt ook gebruikt als die bestaat.
Zonder `SMTP_USER`/`SMTP_PASSWORD` komt er alleen een Telegram-bericht.

## Testen
1. **Actions → SSH woning-monitor → Run workflow → "alleen testbericht"**: er komt een testbericht in Telegram.
2. Nog een keer met **"controle nu"**. De eerste keer wordt het huidige aanbod als nulmeting opgeslagen
   (geen mail). In het log staat bijv. `12 aanbiedingen gevonden (methode: json)`.
3. Staat er `0 aanbiedingen gevonden`? Download onderaan de run het bestand **ssh-debug**
   (schermafbeeldingen en wat de site terugstuurde) en stuur het naar Claude.

## Goed om te weten
- Lukt het drie keer achter elkaar niet om aanbod te lezen, dan krijg je één waarschuwingsmail.
- Een mislukte controle telt nooit als "niets nieuw"; de opgeslagen lijst blijft dan staan.

## Van der Huizen
`vdh_monitor.py` draait in dezelfde workflow (stap **Van der Huizen controleren**) en leest
https://www.vanderhuizen.com/provincies/utrecht/utrecht-1. Bij een nieuwe woning of een
gewijzigde huurprijs komt er een Telegram-bericht én een mail aan Robert en Jasmijn (zelfde
ontvangers en secrets als de SSH-monitor) met huur (kaal en all-in), type, oppervlakte,
beschikbaarheid, doelgroep en een klikbare link naar de advertentie. Kan hij drie keer achter
elkaar niets lezen, dan volgt één waarschuwing via Telegram en mail. Status staat in `vdh_state.json`.

## Meldingen in het kort
| Monitor | Telegram | Mail aan Robert + Jasmijn | Andere ontvangers via |
|---|---|---|---|
| Socius | ✅ | ✅ | `MAIL_TO` |
| SSH | ✅ | ✅ | `MAIL_TO_SSH` |
| Van der Huizen | ✅ | ✅ | `MAIL_TO_SSH` |

Mail werkt alleen als `SMTP_USER` en `SMTP_PASSWORD` zijn ingesteld.


## Hoe het draait (sinds 11-10-2026)
GitHub voert geplande workflows vaak uren te laat uit. Daarom blijft de workflow
**Socius kamer-monitor** zelf doorlopen: hij controleert Socius elke 5 minuten, start
elk uur de **Woning-monitor** (die per dagdeel maar één keer echt controleert) en start
zichzelf na ca. 5u40 opnieuw. De cron-tijden in beide workflows zijn alleen reserve.

- Stoppen: **Actions → Socius kamer-monitor → ⋯ → Disable workflow** (en de lopende run annuleren).
- Weer starten: **Enable workflow** en daarna **Run workflow**.

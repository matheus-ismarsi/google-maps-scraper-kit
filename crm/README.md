# CRM de Leads

A small local CRM for the leads the kit scrapes. You see each lead, qualify it, send a personalized
message on WhatsApp, Instagram or e-mail, and keep the conversation history.

## Open it
- Double-click `crm/abrir-crm.bat`, or
- run `python crm/server.py` (it opens `http://localhost:8765`).

## Where the leads come from
The CRM is not wired to the scraper. It reads the CSV files the `google-maps-scraper` skill writes:
- On the first run it imports `leads.csv` from the kit root.
- After a new scrape, click **Importar** and pick the new CSV, or run
  `python crm/server.py --import novo-leads.csv`. Any kit CSV works, including a plain
  `scripts/scrape.py` export. Leads merge on `place_id` (else the Maps link, else name and address).
  Stages, notes and messages are kept, and blank columns never wipe data you already have.

## How it works
- **Two views:** **Lista** (list and lead panel side by side, arrow keys move between leads) and
  **Funil** (kanban: drag a card to another stage).
- **Numbers on top:** leads, to contact, contacted, response rate, interested, closed. Click a card
  to filter by it.
- **Qualify:** stage (Novo → Contatado → Respondeu → Interessado → Reunião → Fechado / Perdido /
  Não contatar), temperature, follow-up date and notes. The estimated potential (0–5) counts no site
  or social-only presence, a mobile number and the review count. Each lead shows a **next step**
  card with a one-click action for its current stage.
- **Personalized message:** built from the lead's segment (pizzaria, hamburgueria, japonês,
  marmitaria, restaurante), its web presence (no site, Instagram/iFood/linktree only, own site),
  its city and its rating. Edit the copy in **Modelos**.
- **Reply:** with WhatsApp connected (below), messages go out from the CRM and replies arrive in
  **Conversa** by themselves, with sent / delivered / read ticks. Without it, WhatsApp opens in a
  new tab with the text ready. Instagram copies the text and opens the Direct; paste those replies
  under **Conversa** by hand. The first message moves the stage to Contatado and sets a follow-up
  3 days out. A reply moves the stage to Respondeu. A reply of "SAIR" moves it to Não contatar.
- **Exportar:** CSV of the filtered list, `;`-separated for Excel pt-BR.

## Online presence check ("Validar sites")
Google Maps often shows no website for a business that does have a WhatsMenu, iFood or Anota AI
page, and a "website" on Maps is often just a delivery platform. The CRM:
- classifies every Maps website against a list of menu and delivery platforms (`presence.py`,
  `PLATFORMS`: Saipos, InstaDelivery, GplusToGo, MenuDino and others count as "só cardápio", not
  "com site");
- with **Validar sites**, checks each lead without an own website in the background, about 10 s per
  lead: a web search (Bing, then DuckDuckGo) where a link only counts when its address carries the
  business name, direct probes on WhatsMenu and Foodplus, and Linktree pages and short links opened;
- marks what it finds outside Google with **🔎 fora do Google** and uses a dedicated first message
  ("vocês já têm cardápio no WhatsMenu, mas ele não aparece no Google").

In a lead's **Detalhes → Presença online** you see every link and where it came from. Confirm or
reject suggestions, add links by hand, or force the classification. Your decisions survive later
checks. No check is complete: small places with no indexed page, or links that live only in an
Instagram bio, stay "sem site". Review the hot leads before writing to them.

## WhatsApp inside the CRM
Click **WhatsApp** in the top bar. Two ways to connect:

**WhatsApp Web (QR code), the default.** Unofficial: `wa-bridge/bridge.js` runs WhatsApp Web in a
hidden Chrome through [whatsapp-web.js](https://github.com/wwebjs/whatsapp-web.js), linked like any
"Aparelhos conectados" device.
1. Double-click `crm/instalar-whatsapp.bat` once (needs Node.js; it uses the installed Chrome).
2. Reopen the CRM. It starts the bridge by itself and stops it when it closes.
3. Scan the QR code in the WhatsApp panel. The session survives restarts.

This breaks WhatsApp's terms and can get the number banned. The CRM sends one message per click,
shows "typing…" before each send and asks for confirmation past 25 first contacts a day. Keep it
slow and personal. The session lives in `%LOCALAPPDATA%\crm-leads\wa-session` (a short path:
WhatsApp Web's browser database fails past Windows' 260-character limit).

**Official API (Meta).** WhatsApp Business Cloud API (`whatsapp.py`): token, phone number ID,
account ID and app secret in the **API oficial** tab. First messages must be Meta-approved templates
(the panel can submit three suggested ones), Meta charges per template message, and replies need a
public tunnel to `/webhook` (ngrok). A number registered there stops working in the WhatsApp app.
The CRM uses WhatsApp Web when it is connected, else the official API.

## Limits
- Media (photos, audio) received on WhatsApp show as a placeholder: open them on the phone.
- Data lives in `crm/data/` (gitignored) and only on this machine. The server listens on
  `127.0.0.1` only; through a tunnel, only `/webhook` answers. These are personal data: follow the
  LGPD and honor anyone who asks not to be contacted (stage **Não contatar**).

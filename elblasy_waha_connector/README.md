# Elblasy WhatsApp (WAHA) - `elblasy_waha_connector` 20.0.3.1.0

Unified module (it replaces the former `elblasy_waha_connector`, `elblasy_waha_groups`, `elblasy_waha_automation` and `elblasy_waha_groups_hr`).
Target: **Odoo 20.0**. WhatsApp through your own [WAHA](https://waha.devlike.pro) server.

## What is inside
| Area | Models / files |
|---|---|
| HTTP client (no ORM) | `models/waha_client.py` |
| Servers & sessions (QR login, status from WAHA) | `waha_server.py`, `waha_session.py` |
| Inbox (conversations, messages) | `waha_conversation.py`, `waha_message.py`, `static/src/inbox/` |
| Webhook receiver + idempotent event processing | `controllers/main.py`, `waha_webhook_event.py` |
| Groups: sync, whitelist, members, invite, create, posts, opt-in message log | `waha_group*.py`, `wizard/` |
| Instant messages / documents to a number and groups | `wizard/waha_group_send.py` |
| Automation (server action "Send WhatsApp (WAHA)") + throttled queue | `ir_actions_server.py`, `waha_queue.py` |
| Auto-replies, opt-out, reporting | `waha_autoreply.py`, `waha_optout.py`, `views/waha_report_views.xml` |

Optional add-on (separate on purpose, Enterprise only): `elblasy_waha_whatsapp_bridge` (Odoo's `whatsapp` app over WAHA, incl. *New Chat (Discuss)* and free-text messages without templates).
The employee button is part of this module and is created when `hr` is installed.

## Setup
1. WAHA with an API key behind HTTPS. 2. *WhatsApp > Configuration > Servers*: URL + key, **Test Connection**.
3. *Sessions*: create, **Connect**, scan the QR. 4. Set **Odoo Public URL** (WAHA must reach it) and press Connect once
so the webhook (with `X-Odoo-Database` header) is configured. 5. Group features: **Sync Groups**, then tick
"Odoo can post here" on the groups you want.

## Upgrading from the three separate modules
`migrations/20.0.2.0.0` and `20.0.3.0.0` move the XML ids of the absorbed modules to this module and remove their
leftover records from the Apps list; model/table names are unchanged so no data is touched.
Remove the old folders from the addons path before upgrading.

## Security notes
Groups User / Manager / Administrator (`security/`), multi-company rules on every model. API key and HMAC secret are
plain database fields readable only by managers (not encrypted at rest). Raw webhook payloads: administrators only,
purged after 14 days; group messages are stored only for opted-in groups and purged after 90 days.
Automatic messages respect opt-out keywords; sends are rate-limited (10/min per chat) and the queue sends slowly.

## Automation queue
Items are sent at most once: an item is committed as *Interrupted while sending* before its messages go out and
gets its final state afterwards. If the server stops in between, the item stays *Interrupted*: check the chat, then
use **Retry** if needed. *Send now* only triggers the queue cron; sending never runs inside a user request.

## Inbox
Looks and behaves like WhatsApp Web: chat list with filters (All, Unread, labels), WhatsApp names, avatars and
ticks; day separators, bubbles with time and grey/blue ticks, replies (quotes), reactions, edited and deleted
messages, photos, stickers, voice notes, videos, document cards, locations, contact cards and polls; emoji picker,
light and dark themes, one pane at a time on phones. A chat known by its phone number and by its hidden id (LID) is
one conversation. Reactions, edits and deletions need the session webhook to include the new events: press
**Connect** once on existing sessions after upgrading (WAHA restarts the session, no new QR needed).

## Message actions
Each bubble has WhatsApp Web's menu (hover, then the arrow): quick reactions and the emoji picker, message info,
reply (quoted), copy, edit (own text messages, 15 minutes), forward (up to 5 chats of the same number), pin (1, 7 or
30 days), star, add to the contact's notes (chatter note with the files) and delete: *for everyone* (sent messages,
author or WhatsApp manager) or *for me* (hidden in the inbox only; reports keep it).

## Languages
English source with a full Arabic translation (`i18n/ar.po`) of both addons: menus, fields, views, inbox, dashboard
and messages. The inbox follows right-to-left layouts.

## Chat list, groups and media
The chat list mirrors the phone (*WAHA: mirror the WhatsApp chat list*, every 5 minutes and when the inbox is open):
WhatsApp's order, pinned first, archived chats in their own list, names, pictures, unread counts and the last message
of each chat, even when its webhook never reached Odoo. Opening a chat imports the messages Odoo lacks; scrolling up
loads older ones from WhatsApp. Groups appear in the inbox (session option *Groups in the inbox*) with the author of
each message; auto-replies and opt-out keywords never run in groups.

Media policy per session (*Media & storage*): download all files, files up to a size limit (default 10 MB, videos
on demand) or none; group media on demand by default. Files not downloaded show a Download card and are fetched from
WhatsApp when clicked. *Keep media (days)* deletes old files (texts stay); Odoo's filestore stores identical files once.

## Reporting
*WhatsApp ▸ Reporting* (managers): **Waiting for a Reply** (chats whose last customer message is unanswered),
**Messages** (search by phone, contact, chat, agent or text; filters for received/sent, human/automatic, files,
failed, groups, contacts not in Odoo; period picker; group by contact, agent, type, weekday, hour...),
**Customers & Chats** (one line per chat: volumes, first/last contact, response times, "silent for 30+ days" to
re-engage customers, new chats per month), **Peak Hours** (received messages by weekday × hour, company time zone)
and **Agents** (replies and average response time per person and week). Every line opens the chat in the inbox.

## Dashboard and business impact
*Reporting ▸ Dashboard* (last item, never the landing page): today's new chats, received messages, team replies,
chats waiting for a reply, average response time (and share answered within 15 minutes), automatic replies,
documents sent and failures, each compared with yesterday and clickable; last 14 days, today by hour, waiting list,
team today, business from WhatsApp (30 days) and documents sent today.

*Configuration ▸ Tracked Documents* lists the business documents measured against WhatsApp: any model with a date
and a customer (suggested: confirmed sales orders, posted customer invoices, CRM leads, POS orders, helpdesk tickets,
tasks), with an optional amount, a filter and a window in days. *Reporting ▸ Business from WhatsApp* shows, per
document, whether its customer (company included) chatted on WhatsApp in that window, whether the document itself was
sent on WhatsApp, and the amounts. *Reporting ▸ Sent Documents* lists every document sent on WhatsApp from any app
(Send to WhatsApp, automations and, with the bridge, Odoo WhatsApp templates).

## Webhook processing
The webhook checks the signature, stores the event once (idempotent) and processes it right away, each event in its
own savepoint, so the inbox and Discuss show messages within seconds. An event that fails (an ack before its message,
WAHA unreachable for a media file...) stays in *Webhook Events* and the cron *WAHA: process webhook events* retries it
after 2 minutes, up to 5 times.

## Limits
Inbox sends text and files (max 15 MB); inbound media is stored and displayed. Auto-sync is a polling fallback (about 1 minute delay), not a replacement for the webhook. WEBJS is unofficial:
avoid bulk campaigns. National numbers written with a leading 0 get the country code of the contact (or the company);
other numbers need the country code. LID-only chats are not matched to contacts automatically.
The inbox is checked in Chrome by `tests/test_inbox_ui.py` (every kind of message is rendered).

## Tests
`odoo-bin -d <db> --http-port=<free port> -i elblasy_waha_connector --test-tags /elblasy_waha_connector --stop-after-init`
(WAHA is mocked; the HTTP webhook tests need a free port).

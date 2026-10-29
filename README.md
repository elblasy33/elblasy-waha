# Elblasy WhatsApp (WAHA) for Odoo 20

Odoo 20.0 addons that connect Odoo to a [WAHA](https://waha.devlike.pro) (WhatsApp HTTP API) server.

| Addon | What it is |
|---|---|
| `elblasy_waha_connector` | **Elblasy WhatsApp (WAHA)**: sessions with QR login, inbox, groups, instant messages, automation queue, auto-replies, labels, reports. Works on Community and Enterprise. |
| `elblasy_waha_whatsapp_bridge` | Optional, **Enterprise only**: runs Odoo's native WhatsApp app through a WAHA session and lets you message anyone without templates. |

Dependencies of the main addon: `contacts`, `mail`, `bus`, `base_automation`, Python `requests` (see `requirements.txt`).

## Odoo.sh quick start
1. Add this repository to your Odoo.sh project and push to a development branch.
2. Install **Elblasy WhatsApp (WAHA)** (and the bridge if you want Odoo's WhatsApp app over WAHA).
3. *WhatsApp > Configuration > Servers*: WAHA URL + API key, **Test Connection**.
4. *Sessions*: create, **Connect**, scan the QR. Set **Odoo Public URL** on the server (your `*.odoo.com` address) and press **Connect** once so the webhook is registered.
5. Staging branches neutralize cron jobs: use *Sync Messages* or test on development/production for the queue and auto-sync.

Full documentation: `elblasy_waha_connector/README.md`. Tests: `odoo-bin -i elblasy_waha_connector --test-tags /elblasy_waha_connector --stop-after-init`.

License: LGPL-3.

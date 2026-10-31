"""Connection check of a session: finds why messages do not arrive (webhook address, proxy, crons...)."""
import ipaddress
from urllib.parse import urlparse

import requests
from markupsafe import Markup, escape

from odoo import fields, models

from .waha_client import WahaError

OK, WARN, FAIL = 'ok', 'warn', 'fail'
COLORS = {OK: '#1e8e4e', WARN: '#b26b00', FAIL: '#c92a2a'}
ICONS = {OK: 'check_circle', WARN: 'warning', FAIL: 'error'}


class WahaSession(models.Model):
    _inherit = 'elblasy.waha.session'

    check_report = fields.Html("Connection check", readonly=True, sanitize=False, copy=False)
    check_date = fields.Datetime("Last check", readonly=True, copy=False)

    def action_check_connection(self):
        self.ensure_one()
        self._check_manager()
        results = self.sudo()._run_checks()
        self.sudo().write({'check_report': self._render_checks(results), 'check_date': fields.Datetime.now()})
        worst = FAIL if any(r[0] == FAIL for r in results) else WARN if any(r[0] == WARN for r in results) else OK
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'type': {OK: 'success', WARN: 'warning', FAIL: 'danger'}[worst], 'sticky': worst != OK,
            'title': self.env._("Connection check"),
            'message': self.env._("Everything is fine.") if worst == OK else
            self.env._("Problems found: see the Connection check tab of the session."),
            'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}}}

    def action_register_webhook(self):
        """Point WAHA's webhook to this Odoo (WAHA restarts the session; the phone stays linked)."""
        self.ensure_one()
        self._check_manager()
        client = self._client()
        try:
            client.update_session(self.session_name, self._webhook_config())
            self._apply_remote(client.get_session(self.session_name))
        except WahaError as e:
            self._fail(e)
        return self.action_check_connection()

    # ------------------------------------------------------------------
    def _run_checks(self):
        self.ensure_one()
        _ = self.env._
        results = []
        expected = self.webhook_url
        client = self._client()
        # 1. WAHA and the session
        remote = None
        try:
            remote = client.get_session(self.session_name)
            self._apply_remote(remote)
            status = remote.get('status')
            results.append((OK if status == 'WORKING' else FAIL, _("WAHA server"),
                            _("Reachable. Session status: %s.", status) if status == 'WORKING' else
                            _("Reachable, but the session is %s: connect it again (QR code).", status)))
        except WahaError as e:
            results.append((FAIL, _("WAHA server"), _("Odoo cannot reach WAHA: %s", e)))
        # 2. where WAHA sends the events
        if remote is not None:
            hooks = (remote.get('config') or {}).get('webhooks') or []
            urls = [h.get('url') for h in hooks if h.get('url')]
            if not urls:
                results.append((FAIL, _("Webhook on WAHA"), _(
                    "WAHA has no webhook for this session: nothing is pushed to Odoo. Press \"Register webhook\".")))
            elif expected not in urls:
                results.append((FAIL, _("Webhook on WAHA"), _(
                    "WAHA sends the messages to %(other)s, not to this Odoo (%(mine)s). This happens after moving "
                    "to another server or when two Odoo databases use the same WAHA session. Press \"Register "
                    "webhook\" (a WAHA session can only feed one Odoo).", other=", ".join(urls), mine=expected)))
            elif self._webhook_config_differs(remote):
                results.append((WARN, _("Webhook on WAHA"), _(
                    "The address is right but the events or the database header are outdated (reactions, edits "
                    "and deletions may be missing). Press \"Register webhook\".")))
            else:
                results.append((OK, _("Webhook on WAHA"), _("WAHA sends the events to %s.", expected)))
        # 3. the public address of this Odoo
        base = self.server_id._get_webhook_base_url()
        host = urlparse(base).hostname or ''
        local = host in ('localhost', '127.0.0.1') or host.endswith('.local')
        try:
            local = local or not ipaddress.ip_address(host).is_global
        except ValueError:
            pass
        if local:
            results.append((FAIL, _("Public address of Odoo"), _(
                "The webhook uses %s, which WAHA cannot reach. Set \"Odoo Public URL\" on the WAHA server "
                "(e.g. https://your-domain).", base)))
        elif not base.startswith('https://'):
            results.append((WARN, _("Public address of Odoo"), _(
                "%s is not HTTPS: messages travel unencrypted. Use your domain with SSL in \"Odoo Public URL\".",
                base)))
        try:
            response = requests.post(expected, json={}, timeout=10,
                                     headers={'X-Odoo-Database': self.env.cr.dbname, 'X-Webhook-Hmac': 'check'})
            code = response.status_code
            if code in (400, 403):
                results.append((OK, _("Webhook address reachable"), _(
                    "%(url)s answers (HTTP %(code)s for the test call): the proxy forwards it to this database.",
                    url=expected, code=code)))
            elif code == 404:
                results.append((FAIL, _("Webhook address reachable"), _(
                    "%s answers \"not found\": the proxy reaches an Odoo, but not this database (set dbfilter or a "
                    "single database for this domain) or the module is not installed there.", expected)))
            else:
                results.append((FAIL, _("Webhook address reachable"), _(
                    "%(url)s answered HTTP %(code)s: check the reverse proxy (domain, port, SSL).",
                    url=expected, code=code)))
        except requests.exceptions.RequestException as e:
            results.append((FAIL, _("Webhook address reachable"), _(
                "%(url)s cannot be reached from the server (%(error)s): check DNS, the reverse proxy and SSL.",
                url=expected, error=type(e).__name__)))
        # 4. events actually received
        Event = self.env['elblasy.waha.webhook.event']
        last = Event.search([('session_id', '=', self.id)], order='id desc', limit=1)
        failed = Event.search_count([('session_id', '=', self.id), ('state', '=', 'failed')])
        if not last:
            results.append((WARN, _("Events received"), _("No event has ever reached this database.")))
        else:
            results.append((WARN if failed else OK, _("Events received"), _(
                "Last event: %(date)s. Failed events: %(failed)s.",
                date=fields.Datetime.to_string(last.create_date), failed=failed)))
        # 5. scheduled jobs (chat list mirror, retries, queue)
        cron = self.env.ref('elblasy_waha_connector.cron_waha_chat_list', raise_if_not_found=False)
        stale = fields.Datetime.subtract(fields.Datetime.now(), minutes=20)
        if not cron or not cron.active:
            results.append((WARN, _("Scheduled jobs"), _(
                "\"WAHA: mirror the WhatsApp chat list\" is archived: the chat list only refreshes when the inbox "
                "is open.")))
        elif not cron.lastcall or cron.lastcall < stale:
            results.append((FAIL, _("Scheduled jobs"), _(
                "Odoo's scheduled jobs are not running on this server (last run: %s). Start Odoo with workers "
                "and max_cron_threads ≥ 1, without --no-cron.", cron.lastcall or _("never"))))
        else:
            results.append((OK, _("Scheduled jobs"), _("Running (last run %s).",
                                                         fields.Datetime.to_string(cron.lastcall))))
        return results

    def _render_checks(self, results):
        rows = Markup('').join(
            Markup('<tr><td style="padding:8px 10px;width:34px"><i class="oi" data-icon="{icon}" style="color:{color};'
                   'font-size:20px"></i></td><td style="padding:8px 10px;font-weight:600;white-space:nowrap">{title}'
                   '</td><td style="padding:8px 10px">{text}</td></tr>').format(
                icon=ICONS[level], color=COLORS[level], title=title, text=escape(text))
            for level, title, text in results)
        return Markup('<table class="table table-sm mb-0">%s</table>') % rows

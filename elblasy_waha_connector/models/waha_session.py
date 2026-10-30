import json
import logging
import secrets
import time
from datetime import datetime, timezone

import psycopg2

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import BinaryBytes

from .waha_client import WahaError
from .waha_server import MANAGER

_logger = logging.getLogger(__name__)

# WAHA lifecycle; engines may add more, unknown values are stored as-is.
KNOWN_STATUSES = [
    ('STOPPED', "Stopped"),
    ('STARTING', "Starting"),
    ('SCAN_QR_CODE', "Scan QR Code"),
    ('PASSKEY_REQUIRED', "Passkey Required"),
    ('PASSKEY_CONFIRMATION_REQUIRED', "Passkey Confirmation Required"),
    ('WORKING', "Connected"),
    ('FAILED', "Failed"),
]
HINTS = {
    'FAILED': "Try Restart first. If it fails again, Log out and connect again.",
    'PASSKEY_REQUIRED': "WhatsApp requires a passkey: finish the authentication from the WAHA dashboard.",
    'PASSKEY_CONFIRMATION_REQUIRED': "Confirm the code on your phone, finish from the WAHA dashboard.",
}


class WahaSession(models.Model):
    _name = 'elblasy.waha.session'
    _description = "WhatsApp Session"
    _order = 'name'

    name = fields.Char(required=True)
    session_name = fields.Char(
        "WAHA Session Name", required=True, copy=False,
        help="Technical name used in WAHA (letters, digits, - and _).")
    server_id = fields.Many2one('elblasy.waha.server', required=True, ondelete='restrict', index=True)
    company_id = fields.Many2one(related='server_id.company_id', store=True, index=True)
    active = fields.Boolean(default=True)
    status = fields.Selection(KNOWN_STATUSES, readonly=True, copy=False, default='STOPPED')
    status_raw = fields.Char(readonly=True, copy=False)
    status_hint = fields.Char(compute='_compute_status_hint')
    phone_number = fields.Char(readonly=True, copy=False)
    whatsapp_profile_name = fields.Char(readonly=True, copy=False)
    engine = fields.Char(readonly=True, copy=False)
    last_connected_at = fields.Datetime(readonly=True, copy=False)
    last_status_update = fields.Datetime(readonly=True, copy=False)
    last_error = fields.Char(readonly=True, copy=False)
    profile_image = fields.Image(max_width=128, max_height=128, readonly=True, copy=False)
    capping_status = fields.Char(readonly=True, copy=False)
    capping_used = fields.Integer(readonly=True, copy=False)
    capping_total = fields.Integer(readonly=True, copy=False, help="-1 = no quota")
    timelock_active = fields.Boolean(
        "New-contact lock", readonly=True, copy=False,
        help="WhatsApp restricts messaging people you have never chatted with (error 463).")
    timelock_ends = fields.Datetime(readonly=True, copy=False)
    qr_image = fields.Binary(readonly=True, copy=False, attachment=False)
    qr_updated_at = fields.Datetime(readonly=True, copy=False)
    sync_read_state = fields.Boolean(
        "Mark as read on the phone", default=True,
        help="When a chat is read in the Odoo inbox, clear its unread badge on the phone as well.")
    reject_calls = fields.Boolean(
        "Reject incoming calls", help="Decline WhatsApp calls automatically (needs the call.received webhook event).")
    reject_calls_message = fields.Text(
        "Reply to callers", default="Hello! We attend by message only. Please write to us here.")
    label_ids = fields.One2many('elblasy.waha.label', 'session_id')
    check_numbers = fields.Boolean(
        "Verify numbers", default=True,
        help="Before sending to a phone number from the Send WhatsApp window or an automation rule, ask WAHA "
             "whether the number has WhatsApp (sending to unregistered numbers raises the ban risk).")
    auto_sync_messages = fields.Boolean(
        "Auto-sync messages", help="Fetch new incoming messages from WAHA every minute. Use it when the webhook "
        "cannot reach Odoo (local installation); with a working webhook leave it off.")
    messages_synced_at = fields.Datetime("Last Messages Sync", readonly=True, copy=False)
    groups_synced_at = fields.Datetime(readonly=True, copy=False)
    group_ids = fields.One2many('elblasy.waha.group', 'session_id')
    webhook_token = fields.Char(copy=False, readonly=True, index=True, groups=MANAGER,
                                default=lambda s: secrets.token_urlsafe(24))
    webhook_url = fields.Char(compute='_compute_webhook_url', groups=MANAGER)

    _session_name_uniq = models.Constraint(
        'UNIQUE (server_id, session_name)', "This session name already exists on the server.")
    _webhook_token_uniq = models.Constraint('UNIQUE (webhook_token)', "Webhook token must be unique.")

    @api.constrains('session_name')
    def _check_session_name(self):
        for rec in self:
            if not rec.session_name or not all(c.isalnum() or c in '-_' for c in rec.session_name):
                raise UserError(self.env._("Session name may only contain letters, digits, '-' and '_'."))

    @api.depends('status')
    def _compute_status_hint(self):
        for rec in self:
            rec.status_hint = self.env._(HINTS[rec.status]) if rec.status in HINTS else False

    @api.depends('webhook_token', 'server_id.webhook_base_url')
    def _compute_webhook_url(self):
        for rec in self:
            rec.webhook_url = (
                f"{rec.server_id._get_webhook_base_url()}/waha/webhook/{rec.webhook_token}"
                if rec.webhook_token and rec.server_id else False)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _check_manager(self):
        self.server_id._check_manager()

    def _client(self):
        return self.server_id._get_client()

    def _webhook_config(self):
        self.ensure_one()
        server = self.server_id.sudo()
        hook = {
            'url': f"{server._get_webhook_base_url()}/waha/webhook/{self.sudo().webhook_token}",
            'events': self._webhook_events(),
            'retries': {'policy': 'exponential', 'delaySeconds': 2, 'attempts': 8},
            # tells Odoo which database to use when the server hosts several (dbfilter-less setups)
            'customHeaders': [{'name': 'X-Odoo-Database', 'value': self.env.cr.dbname}],
        }
        if server.use_hmac and server.webhook_secret:
            hook['hmac'] = {'key': server.webhook_secret}
        return {'webhooks': [hook]}

    def _webhook_events(self):
        self.ensure_one()
        events = ['session.status', 'message.any', 'message.ack', 'group.v2.update', 'group.v2.participants',
                  'label.upsert', 'label.deleted', 'label.chat.added', 'label.chat.deleted']
        if self.sudo().reject_calls:
            events.append('call.received')
        return events

    def action_sync_labels(self):
        self.ensure_one()
        self._check_manager()
        count = self.env['elblasy.waha.label']._sync_session(self)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': self.env._("Labels"), 'type': 'success', 'message': self.env._("%s label(s) synced.", count)}}

    def _fail(self, error):
        """Record an error (without secrets) and raise a user-friendly message."""
        self._store_last_error(str(error))
        raise UserError(self.env._("WAHA error (%(kind)s): %(msg)s", kind=error.kind, msg=error))

    def _store_last_error(self, message):
        """The UserError raised next rolls the transaction back: write the error in its own short transaction.
        Never waits: if the row is locked by the current request, the error is only shown, not stored."""
        if not self.ids:
            return
        try:
            with self.env.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '200ms'")
                cr.execute("UPDATE elblasy_waha_session SET last_error = %s WHERE id IN %s", [message, tuple(self.ids)])
        except psycopg2.Error as e:
            _logger.info("Could not store the last error of session(s) %s: %s", self.ids, type(e).__name__)
        self.invalidate_recordset(['last_error'])

    def _apply_remote(self, data):
        """Update local fields from a WAHA session object / status payload."""
        self.ensure_one()
        vals = {'last_status_update': fields.Datetime.now()}
        status = data.get('status')
        if status:
            known = {k for k, _ in KNOWN_STATUSES}
            vals['status_raw'] = status
            vals['status'] = status if status in known else False
            if status == 'WORKING' and self.status != 'WORKING':
                vals['last_connected_at'] = fields.Datetime.now()
            if status != 'SCAN_QR_CODE':
                vals.update(qr_image=False, qr_updated_at=False)
            if status in ('WORKING', 'SCAN_QR_CODE', 'STARTING', 'STOPPED'):
                vals['last_error'] = False
        me = data.get('me')
        if isinstance(me, dict) and me.get('id'):
            vals['phone_number'] = ''.join(c for c in me['id'].split('@')[0] if c.isdigit())
            vals['whatsapp_profile_name'] = me.get('pushName') or False
        limits = data.get('limits') if isinstance(data.get('limits'), dict) else (me if isinstance(me, dict) else {})
        capping = limits.get('messageCapping')
        if isinstance(capping, dict):
            vals.update(capping_status=capping.get('cappingStatus'), capping_used=capping.get('usedQuota') or 0,
                        capping_total=capping.get('totalQuota') if capping.get('totalQuota') is not None else -1)
        if 'reachoutTimelock' in limits:
            lock = limits.get('reachoutTimelock') if isinstance(limits.get('reachoutTimelock'), dict) else {}
            vals['timelock_active'] = bool(lock.get('isActive'))
            vals['timelock_ends'] = self._ts_to_datetime(lock.get('timeEnforcementEnds'))
        engine = data.get('engine')
        if isinstance(engine, dict):
            engine = engine.get('engine')
        if engine:
            vals['engine'] = engine
        self.sudo().write(vals)
        self._notify_bus()

    @staticmethod
    def _ts_to_datetime(value):
        if not isinstance(value, (int, float)) or value <= 0:
            return False
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)

    def _new_chat_restricted(self, chat_id):
        """Reason (or False) why a message to ``chat_id`` is risky now: WhatsApp restricts NEW contacts only."""
        self.ensure_one()
        session = self.sudo()
        limited = session.timelock_active or (session.capping_total > 0 and session.capping_used >= session.capping_total)
        if not limited:
            return False
        known = self.env['elblasy.waha.conversation'].sudo().search_count(
            [('session_id', '=', session.id), ('remote_chat_id', '=', chat_id)]) or \
            self.env['elblasy.waha.group.post'].sudo().search_count(
                [('session_id', '=', session.id), ('chat_id', '=', chat_id), ('state', '=', 'sent')])
        if known:
            return False
        return self.env._("WhatsApp is limiting messages to new contacts on this number right now. "
                          "Existing conversations are not affected.")

    def _notify_bus(self):
        for rec in self:
            users = self.env['res.users'].sudo().search([
                ('all_group_ids', 'in', self.env.ref('elblasy_waha_connector.group_waha_user').ids),
                ('company_ids', 'in', rec.company_id.ids)])
            for user in users:
                self.env['bus.bus']._sendone(user, 'elblasy_waha/session', {
                    'session_id': rec.id, 'status': rec.status})

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def action_refresh_status(self):
        """Fetch the real state from WAHA (source of truth)."""
        self._check_manager()
        for rec in self:
            try:
                rec._refresh_status()
            except WahaError as e:
                rec._fail(e)
        return True

    def _refresh_status(self):
        """Fetch the real state from WAHA (no permission check: also used by crons). Raises WahaError."""
        self.ensure_one()
        client = self._client()
        self._apply_remote(client.get_session(self.session_name))
        self._wait_for_qr(client, timeout=8)
        self._refresh_profile(client)

    def _refresh_profile(self, client):
        """Avatar of the connected WhatsApp account (best effort: privacy or engines may not provide it)."""
        self.ensure_one()
        if self.status != 'WORKING' or self.profile_image:
            return
        try:
            profile = client.get(f'/api/{self.session_name}/profile')
            url = profile.get('picture') if isinstance(profile, dict) else None
            if url:
                self.sudo().profile_image = BinaryBytes(client.download_picture(url))
        except WahaError as e:
            _logger.info("Profile picture of session %s not available: %s", self.id, e)

    def _fetch_qr(self, client=None):
        self.ensure_one()
        client = client or self._client()
        try:
            qr = client.get_qr(self.session_name)
        except WahaError as e:
            if e.kind in ('not_found', 'http'):
                return False  # QR not available yet
            raise
        if isinstance(qr, dict) and qr.get('data'):
            self.sudo().write({'qr_image': qr['data'], 'qr_updated_at': fields.Datetime.now()})
            self._notify_bus()
            return True
        return False

    def _webhook_config_differs(self, remote):
        wanted = self._webhook_config()['webhooks'][0]
        hooks = (remote.get('config') or {}).get('webhooks') or []
        current = hooks[0] if hooks else {}
        return any(current.get(k) != wanted.get(k) for k in ('url', 'events', 'customHeaders')) \
            or (current.get('hmac') or {}).get('key') != (wanted.get('hmac') or {}).get('key')

    def _wait_for_qr(self, client, timeout=15):
        """The engine needs a few seconds to boot: poll briefly (bounded) instead of showing an empty dialog."""
        self.ensure_one()
        deadline = time.monotonic() + timeout
        while True:
            if self.status == 'SCAN_QR_CODE' and self._fetch_qr(client):
                return
            if self.status not in ('STARTING', 'SCAN_QR_CODE') or time.monotonic() >= deadline:
                return
            time.sleep(2)
            self._apply_remote(client.get_session(self.session_name))

    def _ensure_remote(self, client):
        """Create the session on WAHA only if it does not exist yet (idempotent)."""
        self.ensure_one()
        try:
            return client.get_session(self.session_name), False
        except WahaError as e:
            if e.kind != 'not_found':
                raise
        return client.create_session(self.session_name, self._webhook_config(), start=False), True

    def action_connect(self):
        """Create (if needed), configure webhooks, start and open the QR dialog."""
        self.ensure_one()
        self._check_manager()
        client = self._client()
        try:
            data, created = self._ensure_remote(client)
            if not created and self._webhook_config_differs(data):
                # PUT restarts a running session in WAHA: only do it when the config really changed
                client.update_session(self.session_name, self._webhook_config())
                data = client.get_session(self.session_name)
            if data.get('status') in (None, 'STOPPED', 'FAILED'):
                client.start_session(self.session_name)
            self._apply_remote(client.get_session(self.session_name))
            self._wait_for_qr(client)
        except WahaError as e:
            self._fail(e)
        return self.action_open_qr()

    def action_open_qr(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': self.env._("Connect WhatsApp"),
            'res_model': self._name, 'res_id': self.id, 'view_mode': 'form',
            'views': [(self.env.ref('elblasy_waha_connector.waha_session_view_form_qr').id, 'form')],
            'target': 'new',
        }

    def action_poll(self):
        """Called by the QR dialog: refresh status and QR."""
        self.action_refresh_status()
        return True

    def _simple_call(self, method):
        self.ensure_one()
        self._check_manager()
        try:
            getattr(self._client(), method)(self.session_name)
            self._apply_remote(self._client().get_session(self.session_name))
        except WahaError as e:
            self._fail(e)
        return True

    def action_start(self):
        return self._simple_call('start_session')

    def action_stop(self):
        return self._simple_call('stop_session')

    def action_restart(self):
        return self._simple_call('restart_session')

    def action_logout(self):
        """Explicit user action only; never triggered automatically."""
        if not self.env.user.has_group('elblasy_waha_connector.group_waha_admin'):
            raise UserError(self.env._("Only WhatsApp administrators can log a session out."))
        return self._simple_call('logout_session')

    def unlink(self):
        if not self.env.user.has_group('elblasy_waha_connector.group_waha_admin'):
            raise UserError(self.env._("Only WhatsApp administrators can delete sessions."))
        return super().unlink()

    def action_sync_groups(self):
        self._check_manager()
        self.ensure_one()
        count = self.env['elblasy.waha.group']._sync_session(self)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': self.env._("Groups"), 'type': 'success',
            'message': self.env._("%s group(s) synced. Tick \"Odoo can post here\" on the ones you want to use.", count)}}

    SYNC_CHATS = 30
    SYNC_MESSAGES = 20

    SYNC_BUDGET_SECONDS = 50  # stay well below the HTTP worker limit (Odoo.sh: 120 s)
    SYNC_MIN_READ_TIMEOUT = 60

    def _sync_messages(self, default_days=2, budget=None):
        """Pull recent messages (also the ones sent from the phone) from WAHA and feed them through the normal
        event pipeline.

        Bounded (30 chats x 20 messages, only messages newer than the last sync) and idempotent (event key
        ``sync_<message id>`` plus the per-message de-duplication of the handlers). A slow or failing chat is
        skipped, the others continue, and the sync point only advances when everything was fetched, so nothing
        is lost: the next run simply retries. Returns ``(imported, skipped_chats)``.
        """
        self.ensure_one()
        client = self._client()
        client.timeout = (client.timeout[0], max(client.timeout[1], self.SYNC_MIN_READ_TIMEOUT))
        deadline = time.monotonic() + (self.SYNC_BUDGET_SECONDS if budget is None else budget)
        since = self.messages_synced_at or fields.Datetime.subtract(fields.Datetime.now(), days=default_days)
        since_ts = int(since.replace(tzinfo=timezone.utc).timestamp())
        Event = self.env['elblasy.waha.webhook.event'].sudo()
        imported = skipped = consecutive_failures = 0
        chats = client.get(f'/api/{self.session_name}/chats/overview', {'limit': self.SYNC_CHATS})  # failure: abort
        for chat in chats if isinstance(chats, list) else []:
            chat_id = chat.get('id')
            chat_id = chat_id.get('_serialized') if isinstance(chat_id, dict) else chat_id
            if not isinstance(chat_id, str) or chat_id.rpartition('@')[2] not in ('c.us', 'lid'):
                continue
            last = chat.get('lastMessage') if isinstance(chat.get('lastMessage'), dict) else {}
            if last.get('timestamp') and last['timestamp'] < since_ts:
                continue  # nothing new in this chat: spare an API call
            if time.monotonic() >= deadline:
                skipped += 1  # out of time: the remaining chats are fetched by the next run
                continue
            try:
                messages = client.get(f'/api/{self.session_name}/chats/{chat_id}/messages', {
                    'limit': self.SYNC_MESSAGES, 'downloadMedia': 'true', 'filter.timestamp.gte': since_ts})
            except WahaError as e:
                _logger.info("Sync of chat %s skipped: %s", chat_id.partition('@')[0][-4:], e)
                skipped += 1
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    skipped += 1  # the server is not answering: stop hammering it
                    break
                continue
            consecutive_failures = 0
            for message in messages if isinstance(messages, list) else []:
                msg_id = message.get('id')
                msg_id = msg_id.get('_serialized') if isinstance(msg_id, dict) else msg_id
                if not msg_id or (message.get('timestamp') or 0) < since_ts:
                    continue
                message['id'] = msg_id
                data = {'id': f'sync_{msg_id}', 'event': 'message.any', 'session': self.session_name,
                        'payload': message}
                event = Event._store_event(self.sudo(), json.dumps(data).encode(), data)
                if event:
                    event._process_event()
                    imported += event.state == 'done'
        if not skipped:
            self.sudo().messages_synced_at = fields.Datetime.now()
        return imported, skipped

    def action_sync_messages(self):
        self.ensure_one()
        self._check_manager()
        try:
            imported, skipped = self._sync_messages()
        except WahaError as e:
            self._fail(e)
        message = self.env._("%s new message(s) imported.", imported)
        if skipped:
            message += " " + self.env._(
                "%s chat(s) were skipped because WAHA was slow; press Sync again, nothing is lost.", skipped)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': self.env._("Messages sync"), 'type': 'warning' if skipped else 'success', 'message': message}}

    @api.model
    def _cron_sync_messages(self):
        for session in self.sudo().search([('auto_sync_messages', '=', True), ('status', '=', 'WORKING')]):
            if not session.messages_synced_at:
                session.messages_synced_at = fields.Datetime.now()  # start from now: never import old history
                continue
            try:
                session._sync_messages(budget=40)
            except WahaError as e:
                _logger.warning("Auto-sync of session %s failed: %s", session.id, e)
                try:
                    session._refresh_status()  # a FAILED/STOPPED session must stop being polled every minute
                except WahaError as e2:
                    _logger.info("Status of session %s unavailable: %s", session.id, e2)

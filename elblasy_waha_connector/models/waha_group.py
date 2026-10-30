import logging
import re

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import BinaryBytes

from odoo.addons.elblasy_waha_connector.models.waha_client import WahaError

_logger = logging.getLogger(__name__)
MANAGER = 'elblasy_waha_connector.group_waha_manager'
SYNC_MIN_INTERVAL = 60  # seconds between two syncs of a session (WAHA rate-limits group listing)


def _serialized(value):
    return value.get('_serialized') if isinstance(value, dict) else value


class WahaGroup(models.Model):
    _name = 'elblasy.waha.group'
    _description = "WhatsApp Group"
    _order = 'name'

    name = fields.Char("Subject", required=True)
    session_id = fields.Many2one('elblasy.waha.session', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='session_id.company_id', store=True, index=True)
    chat_id = fields.Char("Group ID", required=True, readonly=True, copy=False, help="Looks like 123456789@g.us")
    image_128 = fields.Image(max_width=128, max_height=128, readonly=True)
    picture_synced_at = fields.Datetime(readonly=True)
    messages_admin_only = fields.Boolean(readonly=True, help="Only the admins of the group can write in it.")
    info_admin_only = fields.Boolean(readonly=True, help="Only the admins can edit the group info.")
    description = fields.Text(readonly=True)
    participants_count = fields.Integer(readonly=True)
    allow_send = fields.Boolean(
        "Odoo can post here", default=False,
        help="Whitelist: only groups ticked here appear in the send wizard.")
    active = fields.Boolean(default=True)
    last_sync_date = fields.Datetime(readonly=True)
    post_ids = fields.One2many('elblasy.waha.group.post', 'group_id')
    member_ids = fields.One2many('elblasy.waha.group.member', 'group_id')
    members_synced_at = fields.Datetime(readonly=True)
    invite_link = fields.Char(readonly=True, copy=False, groups=MANAGER)
    log_messages = fields.Boolean(
        "Store incoming messages", default=False,
        help="Opt-in: messages written in the group are saved in Odoo (privacy: only enable for groups you own).")
    message_ids = fields.One2many('elblasy.waha.group.message', 'group_id')

    _chat_uniq = models.Constraint('UNIQUE (session_id, chat_id)', "This group is already registered for the session.")

    # ------------------------------------------------------------------
    @api.model
    def _sync_session(self, session):
        """Create/update the groups of ``session`` from WAHA. Returns the number of groups seen."""
        session.ensure_one()
        now = fields.Datetime.now()
        last = session.sudo().groups_synced_at
        if last and (now - last).total_seconds() < SYNC_MIN_INTERVAL:
            raise UserError(self.env._("Groups were synced less than a minute ago, please wait."))
        try:
            data = session.server_id._get_client().get(
                f'/api/{session.session_name}/groups', {'limit': 200, 'exclude': 'participants'})
        except WahaError as e:
            session._fail(e)
        existing = {g.chat_id: g for g in self.sudo().with_context(active_test=False).search(
            [('session_id', '=', session.id)])}
        count = 0
        for item in data if isinstance(data, list) else []:
            chat_id = _serialized(item.get('id'))
            if not isinstance(chat_id, str) or not chat_id.endswith('@g.us'):
                continue
            meta = item.get('groupMetadata') if isinstance(item.get('groupMetadata'), dict) else {}
            participants = item.get('participants') if isinstance(item.get('participants'), list) \
                else meta.get('participants') if isinstance(meta.get('participants'), list) else None
            vals = {
                'name': meta.get('subject') or item.get('subject') or item.get('name') or chat_id,
                'description': meta.get('desc') or item.get('description') or False,
                'participants_count': len(participants) if participants is not None
                else meta.get('size') or item.get('size') or 0,
                'messages_admin_only': bool(meta.get('announce')),
                'info_admin_only': bool(meta.get('restrict')),
                'last_sync_date': now,
            }
            group = existing.get(chat_id)
            if group:
                group.write(vals)
            else:
                self.sudo().create({**vals, 'session_id': session.id, 'chat_id': chat_id})
            count += 1
        session.sudo().groups_synced_at = now
        return count

    def action_open_send_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': self.env._("Send to WhatsApp group"),
            'res_model': 'elblasy.waha.group.send', 'view_mode': 'form', 'target': 'new',
            'context': {'default_group_id': self.id},
        }

    # ------------------------------------------------------------------
    # management (manager only; the API call is the source of truth)
    # ------------------------------------------------------------------
    def _manage(self, method, path, payload=None):
        self.ensure_one()
        self.session_id._check_manager()
        client = self.session_id.server_id._get_client()
        try:
            return getattr(client, method)(f'/api/{self.session_id.session_name}/groups/{self.chat_id}{path}',
                                           *([payload] if payload is not None else []))
        except WahaError as e:
            self.session_id._fail(e)

    @staticmethod
    def _participants_payload(wa_ids):
        return {'participants': [{'id': wa_id} for wa_id in wa_ids]}

    def action_sync_members(self):
        self.ensure_one()
        data = self._manage('get', '/participants/v2')
        Member = self.env['elblasy.waha.group.member'].sudo()
        existing = {m.wa_id: m for m in Member.search([('group_id', '=', self.id)])}
        seen = set()
        for item in data if isinstance(data, list) else []:
            wa_id = _serialized(item.get('id'))
            if not isinstance(wa_id, str):
                continue
            seen.add(wa_id)
            vals = {'role': {'admin': 'admin', 'superadmin': 'superadmin'}.get(item.get('role'), 'participant')}
            if wa_id in existing:
                existing[wa_id].write(vals)
            else:
                Member.create({**vals, 'group_id': self.id, 'wa_id': wa_id})
        Member.browse([m.id for wa_id, m in existing.items() if wa_id not in seen]).unlink()
        self.sudo().write({'members_synced_at': fields.Datetime.now(), 'participants_count': len(seen)})
        return True

    def add_participants(self, partners):
        self.ensure_one()
        ids = []
        for partner in partners:
            digits = re.sub(r'\D', '', partner.phone or '')
            digits = digits[2:] if digits.startswith('00') else digits
            if len(digits) < 7:
                raise UserError(self.env._("%s has no valid phone number.", partner.name))
            ids.append(f"{digits}@c.us")
        self._manage('post', '/participants/add', self._participants_payload(ids))
        return self.action_sync_members()

    def action_get_invite_link(self):
        self.ensure_one()
        data = self._manage('get', '/invite-code')
        code = data if isinstance(data, str) else (data or {}).get('inviteCode') or (data or {}).get('code')
        if not code:
            raise UserError(self.env._("WAHA did not return an invite code (are you an admin of the group?)."))
        code = code.rsplit('/', 1)[-1]
        self.sudo().invite_link = f"https://chat.whatsapp.com/{code}"
        return True

    def action_revoke_invite_link(self):
        self.ensure_one()
        self._manage('post', '/invite-code/revoke', {})
        return self.action_get_invite_link()

    def action_leave(self):
        """Explicit manager action (confirmed in the UI)."""
        self.ensure_one()
        self._manage('post', '/leave', {})
        self.sudo().write({'active': False, 'allow_send': False})
        return True

    def action_open_add_members(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': self.env._("Add members"),
                'res_model': 'elblasy.waha.group.members', 'view_mode': 'form', 'target': 'new',
                'context': {'default_group_id': self.id}}

    PICTURE_REFRESH_DAYS = 7

    def _fetch_picture(self):
        """Store the group picture. Returns False (and keeps the old picture) when WhatsApp has none."""
        self.ensure_one()
        client = self.session_id.sudo().server_id._get_client()
        url = client.picture_url(self.session_id.session_name, self.chat_id, group=True)
        vals = {'picture_synced_at': fields.Datetime.now()}
        if url:
            vals['image_128'] = BinaryBytes(client.download_picture(url))
        self.sudo().write(vals)
        return bool(url)

    def action_refresh_picture(self):
        self.session_id._check_manager()
        for group in self:
            try:
                group._fetch_picture()
            except WahaError as e:
                group.session_id._fail(e)
        return True

    @api.model
    def _cron_sync_pictures(self, limit=20):
        """Pictures are fetched in small batches (one API call each), most useful groups first."""
        stale = fields.Datetime.subtract(fields.Datetime.now(), days=self.PICTURE_REFRESH_DAYS)
        groups = self.sudo().search(
            [('session_id.status', '=', 'WORKING'), '|', ('picture_synced_at', '=', False), ('picture_synced_at', '<', stale)],
            order='allow_send desc, picture_synced_at asc, id', limit=limit)
        for group in groups:
            try:
                group._fetch_picture()
            except WahaError as e:
                if e.kind in ('network', 'auth'):
                    _logger.warning("Group pictures: stopping, %s", e)
                    break  # the server is unreachable: do not hammer it
                group.picture_synced_at = fields.Datetime.now()  # unavailable picture: retry next week

    @api.model
    def _cron_purge_messages(self):
        """Group messages are kept 90 days by default (system parameter elblasy_waha.group_message_days)."""
        days = self.env['ir.config_parameter'].sudo().get_int('elblasy_waha.group_message_days', 90)
        if days > 0:
            limit = fields.Datetime.subtract(fields.Datetime.now(), days=days)
            self.env['elblasy.waha.group.message'].sudo().search([('date', '<', limit)], limit=5000).unlink()

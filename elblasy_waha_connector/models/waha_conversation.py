import base64
import mimetypes
import logging
import uuid

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import BinaryBytes

from .res_partner import digits_only, wa_digits
from .waha_client import WahaError

_logger = logging.getLogger(__name__)
USER = 'elblasy_waha_connector.group_waha_user'


class WahaConversation(models.Model):
    _name = 'elblasy.waha.conversation'
    _description = "WhatsApp Conversation"
    _order = 'last_message_date desc, id desc'

    name = fields.Char(compute='_compute_name', store=True)
    session_id = fields.Many2one('elblasy.waha.session', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='session_id.company_id', store=True, index=True)
    partner_id = fields.Many2one('res.partner', index='btree_not_null', ondelete='set null')
    remote_chat_id = fields.Char("WhatsApp Chat ID", index=True,
                                 help="Identifier used by WhatsApp (…@c.us, …@lid, …@g.us). "
                                      "Filled automatically from the phone number when left empty.")
    chat_type = fields.Selection([('user', "Contact"), ('lid', "Hidden ID"), ('group', "Group"), ('other', "Other")],
                                 compute='_compute_chat_type', store=True)
    remote_phone = fields.Char("Phone",
                               help="Digits only; empty when the chat id does not carry a phone number.")
    display_name_remote = fields.Char("WhatsApp Name")
    last_message_id = fields.Many2one('elblasy.waha.message', ondelete='set null', copy=False)
    last_message_date = fields.Datetime(index=True)
    last_message_preview = fields.Char()
    unread_count = fields.Integer(default=0)
    image_128 = fields.Image(max_width=128, max_height=128, readonly=True)
    picture_synced_at = fields.Datetime(readonly=True)
    label_ids = fields.Many2many('elblasy.waha.label', 'elblasy_waha_conv_label_rel', 'conversation_id', 'label_id')
    awaiting_since = fields.Datetime(readonly=True, help="First unanswered incoming message.")
    last_autoreply_date = fields.Datetime(readonly=True)
    active = fields.Boolean(default=True)
    message_ids = fields.One2many('elblasy.waha.message', 'conversation_id')

    _chat_uniq = models.Constraint(
        'UNIQUE (session_id, remote_chat_id)', "A conversation already exists for this chat.")

    @api.model_create_multi
    def create(self, vals_list):
        """Allow starting a chat manually from a phone number or a contact."""
        for vals in vals_list:
            if vals.get('remote_chat_id'):
                continue
            phone = wa_digits(vals.get('remote_phone'), self.env.company.country_id)
            if not phone and vals.get('partner_id'):
                phone = self.env['res.partner'].browse(vals['partner_id'])._waha_phone()
            if not phone:
                raise UserError(self.env._("Enter a phone number (with country code) or choose a contact that has one."))
            vals['remote_chat_id'] = f"{phone}@c.us"
            vals['remote_phone'] = phone
        return super().create(vals_list)

    @api.depends('partner_id.name', 'display_name_remote', 'remote_phone', 'remote_chat_id')
    def _compute_name(self):
        for conv in self:
            conv.name = (conv.partner_id.name or conv.display_name_remote
                         or conv.remote_phone or conv.remote_chat_id)

    @api.depends('remote_chat_id')
    def _compute_chat_type(self):
        for conv in self:
            suffix = (conv.remote_chat_id or '').rpartition('@')[2]
            conv.chat_type = {'c.us': 'user', 's.whatsapp.net': 'user', 'lid': 'lid', 'g.us': 'group'}.get(suffix, 'other')

    # ------------------------------------------------------------------
    # creation / partner matching
    # ------------------------------------------------------------------
    @api.model
    def _phone_from_chat_id(self, chat_id):
        local, _sep, suffix = (chat_id or '').rpartition('@')
        return digits_only(local) if suffix in ('c.us', 's.whatsapp.net') else False

    @api.model
    def _get_or_create(self, session, chat_id, display_name=None):
        conv = self.with_context(active_test=False).search(
            [('session_id', '=', session.id), ('remote_chat_id', '=', chat_id)], limit=1)
        if conv:
            if display_name and not conv.display_name_remote:
                conv.display_name_remote = display_name
            return conv
        phone = self._phone_from_chat_id(chat_id)
        conv = self.create({
            'session_id': session.id, 'remote_chat_id': chat_id, 'remote_phone': phone,
            'display_name_remote': display_name or False,
        })
        if conv.chat_type == 'lid':
            conv._resolve_lid_phone()
        partner = conv._match_partner()
        if partner:
            conv.partner_id = partner
        return conv

    def _resolve_lid_phone(self):
        """Ask WAHA which phone number hides behind a ``@lid`` chat (when it knows it)."""
        self.ensure_one()
        try:
            phone = self.session_id.sudo().server_id._get_client().lid_to_phone(
                self.session_id.session_name, self.remote_chat_id)
        except WahaError:
            return False  # the chat stays reachable through its LID; matching can be retried later
        if phone:
            self.sudo().remote_phone = phone
        return bool(phone)

    PICTURE_REFRESH_DAYS = 7

    def _fetch_picture(self):
        self.ensure_one()
        client = self.session_id.sudo().server_id._get_client()
        url = client.picture_url(self.session_id.session_name, self.remote_chat_id)
        vals = {'picture_synced_at': fields.Datetime.now()}
        if url:
            vals['image_128'] = BinaryBytes(client.download_picture(url))
        self.sudo().write(vals)
        return bool(url)

    @api.model
    def _cron_sync_pictures(self, limit=20):
        stale = fields.Datetime.subtract(fields.Datetime.now(), days=self.PICTURE_REFRESH_DAYS)
        convs = self.sudo().search(
            [('chat_type', 'in', ('user', 'lid')), ('session_id.status', '=', 'WORKING'),
             '|', ('picture_synced_at', '=', False), ('picture_synced_at', '<', stale)],
            order='last_message_date desc', limit=limit)
        for conv in convs:
            try:
                conv._fetch_picture()
            except WahaError as e:
                if e.kind in ('network', 'auth'):
                    break
                conv.picture_synced_at = fields.Datetime.now()

    def action_resolve_lid(self):
        """Manual retry from the conversation form."""
        self.ensure_one()
        self._check_user()
        if self._resolve_lid_phone():
            partner = self._match_partner()
            if partner and not self.partner_id:
                self.sudo().partner_id = partner
        return True

    def _match_partner(self):
        """Return the single partner whose phone matches, else empty (never guesses on names)."""
        self.ensure_one()
        digits = self.remote_phone
        if not digits or len(digits) < 7:
            return self.env['res.partner']
        tail = digits[-9:]
        candidates = self.env['res.partner'].sudo().search([('waha_phone_digits', 'like', tail)])
        matches = candidates.filtered(
            lambda p: p.waha_phone_digits.endswith(tail) and (
                digits.endswith(p.waha_phone_digits[-9:]) or p.waha_phone_digits.endswith(digits)))
        return matches if len(matches) == 1 else self.env['res.partner']

    def action_create_partner(self):
        self.ensure_one()
        self._check_user()
        if self.partner_id:
            raise UserError(self.env._("This conversation is already linked to a contact."))
        partner = self.env['res.partner'].create({
            'name': self.display_name_remote or self.remote_phone or self.remote_chat_id,
            'phone': f"+{self.remote_phone}" if self.remote_phone else False,
        })
        self.sudo().partner_id = partner
        return partner.id

    def action_link_partner(self, partner_id):
        self.ensure_one()
        self._check_user()
        partner = self.env['res.partner'].browse(partner_id).exists()
        partner.check_access('read')
        self.sudo().partner_id = partner
        return True

    def action_open_partner(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'res.partner',
                'res_id': self.partner_id.id, 'view_mode': 'form', 'views': [(False, 'form')]}

    # ------------------------------------------------------------------
    # inbox API (used by the OWL client action, ORM rules apply)
    # ------------------------------------------------------------------
    def _check_user(self):
        """Users only read conversations (security): entry points check the user and the record rules, then
        write as the system."""
        if not self.env.user.has_group(USER):
            raise AccessError(self.env._("You are not allowed to use WhatsApp."))
        self.check_access('read')

    @api.model
    def inbox_conversations(self, session_id=None, search=None, limit=40, offset=0, label_id=None):
        self._check_user()
        domain = [('chat_type', '!=', 'group')]
        if session_id:
            domain.append(('session_id', '=', session_id))
        if label_id:
            domain.append(('label_ids', 'in', [label_id]))
        if search:
            domain += ['|', '|', ('name', 'ilike', search), ('remote_phone', 'ilike', search),
                       ('partner_id.name', 'ilike', search)]
        convs = self.search(domain, limit=limit, offset=offset)
        return [{
            'id': c.id, 'name': c.name, 'phone': c.remote_phone, 'partner_id': c.partner_id.id,
            'partner_name': c.partner_id.name, 'session_id': c.session_id.id,
            'session_name': c.session_id.name, 'session_status': c.session_id.status,
            'last_message': c.last_message_preview, 'last_date': fields.Datetime.to_string(c.last_message_date),
            'unread': c.unread_count, 'has_image': bool(c.image_128),
            'labels': [{'id': l.id, 'name': l.name, 'color': l.color_hex} for l in c.label_ids],
            'image_ts': fields.Datetime.to_string(c.picture_synced_at) or '',
        } for c in convs]

    @api.model
    def inbox_sessions(self):
        self._check_user()
        return [{'id': s.id, 'name': s.name, 'status': s.status}
                for s in self.env['elblasy.waha.session'].search([])]

    def inbox_messages(self, before_id=None, limit=30):
        """Newest ``limit`` messages (older than ``before_id``), returned oldest first."""
        self.ensure_one()
        self._check_user()
        domain = [('conversation_id', '=', self.id)]
        if before_id:
            domain.append(('id', '<', before_id))
        msgs = self.env['elblasy.waha.message'].search(domain, order='date desc, id desc', limit=limit)
        return {
            'messages': [m._to_ui() for m in reversed(msgs)],
            'has_more': len(msgs) == limit,
        }

    def inbox_mark_read(self):
        self.ensure_one()
        self._check_user()
        if self.unread_count:
            self.sudo().unread_count = 0
            self._mark_read_on_phone()
        return True

    def _mark_read_on_phone(self):
        """Clear the unread badge on the phone too (best effort: WhatsApp state is not business critical)."""
        session = self.session_id.sudo()
        if not session.sync_read_state or session.status != 'WORKING':
            return
        try:
            session.server_id._get_client().post(
                f'/api/{session.session_name}/chats/{self.remote_chat_id}/messages/read', {})
        except WahaError as e:
            _logger.info("Could not mark chat %s as read on the phone: %s", self.id, e)

    @api.model
    def inbox_labels(self, session_id=None):
        self._check_user()
        domain = [('session_id', '=', session_id)] if session_id else []
        return [{'id': l.id, 'name': l.name, 'color': l.color_hex, 'session_id': l.session_id.id}
                for l in self.env['elblasy.waha.label'].search(domain)]

    def inbox_toggle_label(self, label_id):
        """Attach/detach a WhatsApp label to this chat (written on the phone first, then locally)."""
        self.ensure_one()
        self._check_user()
        label = self.env['elblasy.waha.label'].browse(label_id).exists()
        if not label or label.session_id != self.session_id:
            raise UserError(self.env._("This label does not belong to the session of the chat."))
        labels = self.label_ids - label if label in self.label_ids else self.label_ids | label
        session = self.session_id.sudo()
        try:
            session.server_id._get_client().put(
                f'/api/{session.session_name}/labels/chats/{self.remote_chat_id}',
                {'labels': [{'id': l.label_id} for l in labels]})
        except WahaError as e:
            raise UserError(self.env._("WhatsApp refused the label change (%s). Labels need a WhatsApp Business account.", e))
        self.sudo().label_ids = [(6, 0, labels.ids)]
        return [{'id': l.id, 'name': l.name, 'color': l.color_hex} for l in self.label_ids]

    def inbox_send_text(self, text, client_token=None):
        """Send a text message. Idempotent on ``client_token`` (double click safe)."""
        self.ensure_one()
        self._check_user()
        text = (text or '').strip()
        if not text:
            raise UserError(self.env._("The message is empty."))
        return self._send_text(text, client_token, user=self.env.user)._to_ui()

    def _send_text(self, text, client_token=None, user=None, is_auto=False):
        """Create the message, send it through WAHA and record the outcome (idempotent on ``client_token``)."""
        self.ensure_one()
        Message = self.env['elblasy.waha.message']
        client_token = client_token or uuid.uuid4().hex
        existing = Message.sudo().search([('conversation_id', '=', self.id), ('client_token', '=', client_token)], limit=1)
        if existing:
            return existing
        session = self.session_id
        if session.status != 'WORKING':
            raise UserError(self.env._("The WhatsApp session is not connected (status: %s).", session.status or '?'))
        # messages are written by the system only: users read them (see security), access was checked by the caller
        message = Message.sudo().create({
            'conversation_id': self.id, 'direction': 'out', 'message_type': 'text', 'body': text,
            'date': fields.Datetime.now(), 'delivery_status': 'pending', 'client_token': client_token,
            'sender_identifier': user.name if user else self.env._("Auto-reply"),
            'user_id': user.id if user else False, 'is_auto': is_auto,
        })
        client = session.sudo().server_id._get_client()
        message._deliver(lambda: client.send_text(session.session_name, self.remote_chat_id, text))
        return message

    MAX_FILE_BYTES = 15 * 1024 * 1024

    def inbox_send_file(self, filename, mimetype, data_b64, caption=None, client_token=None):
        """Send an uploaded file (image, video, audio, document) from the inbox."""
        self.ensure_one()
        self._check_user()
        try:
            raw = base64.b64decode(data_b64 or '', validate=True)
        except (ValueError, TypeError):
            raise UserError(self.env._("The file could not be read."))
        if not raw:
            raise UserError(self.env._("The file is empty."))
        if len(raw) > self.MAX_FILE_BYTES:
            raise UserError(self.env._("The file is too large (maximum %s MB).", self.MAX_FILE_BYTES // 1024 // 1024))
        session = self.session_id
        if session.status != 'WORKING':
            raise UserError(self.env._("The WhatsApp session is not connected (status: %s).", session.status or '?'))
        Message = self.env['elblasy.waha.message']
        client_token = client_token or uuid.uuid4().hex
        existing = Message.sudo().search([('conversation_id', '=', self.id), ('client_token', '=', client_token)], limit=1)
        if existing:
            return existing._to_ui()
        filename = (filename or 'file').strip()[:128] or 'file'
        mimetype = (mimetype or mimetypes.guess_type(filename)[0] or 'application/octet-stream').split(';')[0]
        kind = mimetype.split('/')[0]
        message = Message.sudo().create({
            'conversation_id': self.id, 'direction': 'out', 'body': (caption or '').strip() or False,
            'message_type': kind if kind in ('image', 'video', 'audio') else 'document',
            'date': fields.Datetime.now(), 'delivery_status': 'pending', 'client_token': client_token,
            'sender_identifier': self.env.user.name, 'user_id': self.env.user.id,
        })
        attachment = self.env['ir.attachment'].sudo().create({
            'name': filename, 'raw': raw, 'mimetype': mimetype,
            'res_model': 'elblasy.waha.message', 'res_id': message.id})
        path, file = self.env['elblasy.waha.group.post']._attachment_payload(attachment)
        payload = {'session': session.session_name, 'chatId': self.remote_chat_id, 'file': file}
        if message.body:
            payload['caption'] = message.body
        client = session.sudo().server_id._get_client()
        message._deliver(lambda: client.post(path, payload))
        return message._to_ui()

    # ------------------------------------------------------------------
    # incoming hook: opt-out keywords and auto-replies (inbox sessions only)
    # ------------------------------------------------------------------
    def _after_incoming(self, message):
        self.ensure_one()
        if message.message_type != 'text' or not message.body:
            return
        text = message.body.strip().casefold()
        Optout = self.env['elblasy.waha.optout'].sudo()
        if self.remote_phone:
            if text in Optout._keywords('optout'):
                Optout._opt_out(self.company_id, self.remote_phone)
                self._autoreply_send(Optout._reply('optout_reply'), None)
                return
            if text in Optout._keywords('optin'):
                Optout._opt_in(self.company_id, self.remote_phone)
                return
        if (fields.Datetime.now() - message.date).total_seconds() > 600:
            return  # imported history / late delivery: never answer old messages
        rule = self.env['elblasy.waha.autoreply'].sudo()._match(self, message)
        if rule:
            self._autoreply_send(rule._render(self), rule)

    def _autoreply_send(self, text, rule):
        self.ensure_one()
        if not text or self.session_id.status != 'WORKING':
            return
        self.sudo()._send_text(text, user=None, is_auto=True)
        self.sudo().last_autoreply_date = fields.Datetime.now()

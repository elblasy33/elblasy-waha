import base64
import mimetypes
import re
import uuid

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from odoo.addons.elblasy_waha_connector.models.waha_client import WahaError
from odoo.addons.elblasy_waha_connector.models.waha_message import extract_message_id

USER = 'elblasy_waha_connector.group_waha_user'
MAX_POSTS_PER_MINUTE = 10  # per group, a guard against flooding (WhatsApp may ban the number)


class WahaGroupPost(models.Model):
    """Audit trail of everything Odoo posted to a WhatsApp group."""
    _name = 'elblasy.waha.group.post'
    _description = "WhatsApp Group Post"
    _order = 'id desc'

    group_id = fields.Many2one('elblasy.waha.group', ondelete='set null', index='btree_not_null')
    partner_id = fields.Many2one('res.partner', string="Customer chat", ondelete='set null', index='btree_not_null')
    session_id = fields.Many2one('elblasy.waha.session', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='session_id.company_id', store=True, index=True)
    chat_id = fields.Char("Target chat", required=True, index=True)
    target_name = fields.Char(compute='_compute_target_name')
    user_id = fields.Many2one('res.users', default=lambda s: s.env.user, readonly=True)
    res_model = fields.Char(index=True)
    res_id = fields.Many2oneReference(model_field='res_model', index=True)
    body = fields.Text()
    attachment_ids = fields.Many2many('ir.attachment', string="Attachments")
    state = fields.Selection(
        [('pending', "Sending"), ('sent', "Sent"), ('failed', "Failed"), ('unknown', "Not confirmed")],
        default='pending', index=True)
    error = fields.Char()
    wa_message_ids = fields.Char("WhatsApp message ids")
    client_token = fields.Char(copy=False, index='btree_not_null')

    _client_token_uniq = models.Constraint(
        'UNIQUE (session_id, chat_id, client_token)', "Duplicate send request.")

    @api.depends('group_id.name', 'partner_id.name', 'chat_id')
    def _compute_target_name(self):
        for post in self:
            post.target_name = post.group_id.name or post.partner_id.name or post.chat_id

    # ------------------------------------------------------------------
    @api.model
    def _attachment_payload(self, attachment):
        name = attachment.name or 'file'
        mimetype = attachment.mimetype or mimetypes.guess_type(name)[0] or 'application/octet-stream'
        kind = mimetype.split('/')[0]
        path = {'image': '/api/sendImage', 'video': '/api/sendVideo'}.get(kind, '/api/sendFile')
        if mimetype == 'image/svg+xml':
            path = '/api/sendFile'
        return path, {'mimetype': mimetype, 'filename': name, 'data': base64.b64encode(attachment.raw).decode()}

    @api.model
    def send_to_group(self, group, body, attachments=None, res_model=None, res_id=None, client_token=None):
        """Post to a whitelisted WhatsApp group."""
        group.ensure_one()
        if not group.allow_send or not group.active:
            raise UserError(self.env._("Odoo is not allowed to post to this group."))
        return self._send(group.session_id, group.chat_id, body, attachments, res_model, res_id, client_token,
                          group=group)

    @api.model
    def send_to_partner(self, partner, session, body, attachments=None, res_model=None, res_id=None,
                        client_token=None):
        return self.send_to_number(partner.phone, session, body, attachments, res_model, res_id, client_token,
                                   partner=partner)

    @api.model
    def send_to_number(self, phone, session, body, attachments=None, res_model=None, res_id=None,
                       client_token=None, partner=None):
        """Send to a private WhatsApp chat (phone in international format)."""
        digits = re.sub(r'\D', '', phone or '')
        digits = digits[2:] if digits.startswith('00') else digits
        if len(digits) < 7:
            raise UserError(self.env._("Invalid phone number (use the international format with country code)."))
        if not self.env.context.get('waha_ignore_optout') and \
                self.env['elblasy.waha.optout']._is_opted_out(session.company_id, digits):
            raise UserError(self.env._("This number asked to stop receiving automatic messages."))
        chat_id = f"{digits}@c.us"
        if not self.env.context.get('waha_ignore_limits') and (reason := session._new_chat_restricted(chat_id)):
            raise UserError(reason)
        if session.check_numbers:
            chat_id = self._verified_chat_id(session, digits, chat_id)
        return self._send(session, chat_id, body, attachments, res_model, res_id, client_token, partner=partner)

    @api.model
    def _verified_chat_id(self, session, digits, default_chat_id):
        """Ask WAHA if the number has WhatsApp and use the chat id it reports (phone or LID)."""
        try:
            exists, chat_id = session.sudo().server_id._get_client().check_exists(session.session_name, digits)
        except WahaError as e:
            if e.kind in ('not_found', 'http'):
                return default_chat_id  # this WAHA engine/version has no such endpoint: do not block sending
            session.sudo()._fail(e)
        if not exists:
            raise UserError(self.env._("+%s is not registered on WhatsApp.", digits))
        return chat_id if isinstance(chat_id, str) and chat_id.rpartition('@')[2] in ('c.us', 'lid') else default_chat_id

    @api.model
    def _record_targets(self, record):
        """``(partner, phone)`` to message for any record (contact, employee, sale order, invoice...)."""
        partner = self.env['res.partner']
        if record._name == 'res.partner':
            partner = record
        else:
            for fname in ('partner_id', 'work_contact_id'):
                if fname in record._fields and record._fields[fname].comodel_name == 'res.partner' and record[fname]:
                    partner = record[fname]
                    break
        phone = ''
        for fname in ('mobile_phone', 'work_phone', 'phone', 'mobile'):
            if fname in record._fields and record._fields[fname].type == 'char' and record[fname]:
                phone = record[fname]
                break
        return partner, phone or partner.phone or ''

    @api.model
    def _send(self, session, chat_id, body, attachments, res_model, res_id, client_token, group=None, partner=None):
        if not self.env.user.has_group(USER):
            raise AccessError(self.env._("You are not allowed to use WhatsApp."))
        body = (body or '').strip()
        attachments = attachments or self.env['ir.attachment']
        if not body and not attachments:
            raise UserError(self.env._("Write a message or attach a file."))
        if session.status != 'WORKING':
            raise UserError(self.env._("The WhatsApp session is not connected."))
        client_token = client_token or uuid.uuid4().hex
        existing = self.search([('session_id', '=', session.id), ('chat_id', '=', chat_id),
                                ('client_token', '=', client_token)], limit=1)
        if existing:
            return existing
        since = fields.Datetime.subtract(fields.Datetime.now(), minutes=1)
        if self.sudo().search_count([('session_id', '=', session.id), ('chat_id', '=', chat_id),
                                     ('create_date', '>=', since)]) >= MAX_POSTS_PER_MINUTE:
            raise UserError(self.env._("Too many messages to this chat in the last minute, please wait."))
        post = self.create({
            'session_id': session.id, 'chat_id': chat_id,
            'group_id': group.id if group else False, 'partner_id': partner.id if partner else False,
            'body': body, 'attachment_ids': [(6, 0, attachments.ids)],
            'res_model': res_model or False, 'res_id': res_id or False, 'client_token': client_token,
        }).sudo()
        post._deliver()
        post._log_on_record()
        return post

    def _deliver(self):
        self.ensure_one()
        session = self.session_id.sudo()
        client = session.server_id._get_client()
        base = {'session': session.session_name, 'chatId': self.chat_id}
        ids = []
        try:
            if not self.attachment_ids:
                ids.append(extract_message_id(client.post('/api/sendText', {**base, 'text': self.body})))
            for index, attachment in enumerate(self.attachment_ids.sudo()):
                path, file = self._attachment_payload(attachment)
                payload = {**base, 'file': file}
                if index == 0 and self.body:
                    payload['caption'] = self.body
                ids.append(extract_message_id(client.post(path, payload)))
        except WahaError as e:
            # network/unparsable answers are ambiguous: it may have been delivered, never auto-retry
            state = 'unknown' if e.kind in ('network', 'response') else 'failed'
            self.write({'state': state, 'error': str(e), 'wa_message_ids': ','.join(i for i in ids if i)})
        else:
            self.write({'state': 'sent', 'error': False, 'wa_message_ids': ','.join(i for i in ids if i)})

    def _log_on_record(self):
        """Leave a trace in the chatter of the source document."""
        self.ensure_one()
        if not (self.res_model and self.res_id and self.res_model in self.env):
            return
        record = self.env[self.res_model].sudo().browse(self.res_id).exists()
        if not record or not hasattr(record, 'message_post'):
            return
        status = {'sent': self.env._("sent"), 'failed': self.env._("failed"),
                  'unknown': self.env._("not confirmed")}.get(self.state, self.state)
        record.message_post(
            body=Markup("%s <b>%s</b> (%s)") % (
                self.env._("WhatsApp message to"), self.target_name, status),
            attachment_ids=self.attachment_ids.ids, subtype_xmlid='mail.mt_note')

from datetime import datetime, timezone

from odoo import api, fields, models

from .waha_client import WahaError

ACK_RANK = {'pending': 0, 'unknown': 0, 'failed': 0, 'sent': 1, 'delivered': 2, 'read': 3}
# WAHA ack: -1 ERROR, 0 PENDING, 1 SERVER, 2 DEVICE, 3 READ, 4 PLAYED
ACK_MAP = {-1: 'failed', 0: 'pending', 1: 'sent', 2: 'delivered', 3: 'read', 4: 'read'}
ACK_NAME_MAP = {'ERROR': 'failed', 'PENDING': 'pending', 'SERVER': 'sent', 'DEVICE': 'delivered',
                'READ': 'read', 'PLAYED': 'read'}


def extract_message_id(response):
    """WAHA returns the id as a string or as {'_serialized': ...} depending on the engine."""
    if not isinstance(response, dict):
        return False
    mid = response.get('id')
    if isinstance(mid, dict):
        mid = mid.get('_serialized') or mid.get('id')
    return mid if isinstance(mid, str) and mid else False


class WahaMessage(models.Model):
    _name = 'elblasy.waha.message'
    _description = "WhatsApp Message"
    _order = 'date desc, id desc'

    conversation_id = fields.Many2one('elblasy.waha.conversation', required=True, ondelete='cascade', index=True)
    session_id = fields.Many2one(related='conversation_id.session_id', store=True, index=True)
    company_id = fields.Many2one(related='conversation_id.company_id', store=True, index=True)
    remote_message_id = fields.Char(index=True, copy=False)
    client_token = fields.Char(copy=False, index='btree_not_null')
    direction = fields.Selection([('in', "Incoming"), ('out', "Outgoing")], required=True)
    message_type = fields.Selection(
        [('text', "Text"), ('image', "Image"), ('video', "Video"), ('audio', "Audio"),
         ('document', "Document"), ('sticker', "Sticker"), ('location', "Location"), ('contact', "Contact card"),
         ('poll', "Poll"), ('other', "Other")], required=True, default='text')
    body = fields.Text()
    latitude = fields.Float(digits=(10, 7))
    longitude = fields.Float(digits=(10, 7))
    quoted_remote_id = fields.Char("Reply to (id)", copy=False)
    quoted_body = fields.Char("Reply to")
    reactions = fields.Json(copy=False, help="Emoji reactions by sender: {sender: emoji}.")
    is_forwarded = fields.Boolean("Forwarded", readonly=True)
    is_edited = fields.Boolean("Edited", readonly=True)
    is_revoked = fields.Boolean("Deleted for everyone", readonly=True)
    is_starred = fields.Boolean("Starred", readonly=True)
    is_pinned = fields.Boolean("Pinned", readonly=True)
    hidden = fields.Boolean("Deleted for me", readonly=True,
                            help="Removed from the inbox by a user; kept in the reports for the record.")
    date = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    delivery_status = fields.Selection(
        [('pending', "Sending"), ('sent', "Sent"), ('delivered', "Delivered"), ('read', "Read"),
         ('failed', "Failed"), ('unknown', "Not confirmed")], copy=False)
    sender_identifier = fields.Char()
    author_name = fields.Char("Author", help="Name of the group member who wrote the message.")
    media_pending = fields.Boolean(
        "Media not downloaded", readonly=True,
        help="The file stays on WhatsApp until someone opens it (media policy of the session).")
    user_id = fields.Many2one('res.users', string="Sent by", readonly=True, index='btree_not_null')
    is_auto = fields.Boolean("Automatic reply", readonly=True)
    response_seconds = fields.Integer(
        "Response time (s)", readonly=True, aggregator='avg',
        help="Outgoing human replies: seconds since the customer's first unanswered message.")
    failure_reason = fields.Char()
    attachment_ids = fields.One2many(
        'ir.attachment', 'res_id', domain=lambda self: [('res_model', '=', self._name)], string="Files")
    webhook_event_id = fields.Many2one('elblasy.waha.webhook.event', ondelete='set null', copy=False)

    _remote_uniq = models.Constraint(
        'UNIQUE (session_id, remote_message_id)', "This WhatsApp message is already stored.")
    _client_token_uniq = models.Constraint(
        'UNIQUE (conversation_id, client_token)', "Duplicate send request.")

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        for message in messages:
            message._touch_conversation()
        return messages

    def _preview(self):
        self.ensure_one()
        return (self.body or f"[{self.message_type}]")[:120]

    EDIT_MINUTES = 15  # WhatsApp lets people edit their messages for 15 minutes

    def _can_edit(self):
        self.ensure_one()
        return (self.direction == 'out' and self.message_type == 'text' and not self.is_revoked and not self.is_auto
                and bool(self.remote_message_id) and self.delivery_status != 'failed'
                and (fields.Datetime.now() - self.date).total_seconds() < self.EDIT_MINUTES * 60)

    def _set_reaction(self, sender, emoji):
        """WhatsApp keeps one reaction per person; an empty emoji removes it."""
        self.ensure_one()
        reactions = dict(self.reactions or {})
        if emoji:
            reactions[sender] = emoji
        else:
            reactions.pop(sender, None)
        self.sudo().reactions = reactions
        self._notify_bus()

    def _touch_conversation(self):
        self.ensure_one()
        conv = self.conversation_id.sudo()
        vals = {}
        if not conv.last_message_date or self.date >= conv.last_message_date:
            vals.update(last_message_id=self.id, last_message_date=self.date,
                        last_message_preview=self._preview())
        history = self.env.context.get('waha_history')
        if history:
            pass  # imported history: the unread count comes from the phone, nobody is waiting for an answer
        elif self.direction == 'in':
            vals['unread_count'] = conv.unread_count + 1
            if not conv.awaiting_since:
                vals['awaiting_since'] = self.date
        elif not self.is_auto and conv.awaiting_since:
            self.sudo().response_seconds = max(int((self.date - conv.awaiting_since).total_seconds()), 0)
            vals['awaiting_since'] = False
        if vals:
            conv.write(vals)
        if not history:
            self._notify_bus()

    def _notify_bus(self):
        self.ensure_one()
        users = self.env['res.users'].sudo().search([
            ('all_group_ids', 'in', self.env.ref('elblasy_waha_connector.group_waha_user').ids),
            ('company_ids', 'in', self.company_id.ids)])
        payload = {'conversation_id': self.conversation_id.id, 'message_id': self.id}
        for user in users:
            self.env['bus.bus']._sendone(user, 'elblasy_waha/message', payload)

    def _to_ui(self):
        self.ensure_one()
        counts = {}
        for sender, emoji in (self.reactions or {}).items():
            entry = counts.setdefault(emoji, {'emoji': emoji, 'count': 0, 'mine': False})
            entry['count'] += 1
            entry['mine'] = entry['mine'] or sender == 'me'
        return {
            'id': self.id, 'direction': self.direction, 'type': self.message_type, 'body': self.body or '',
            'date': fields.Datetime.to_string(self.date), 'status': self.delivery_status,
            'failure': self.failure_reason, 'sender': self.sender_identifier, 'is_auto': self.is_auto,
            'author': self.author_name or '', 'media_pending': self.media_pending,
            'quoted': self.quoted_body or '', 'forwarded': self.is_forwarded, 'edited': self.is_edited,
            'revoked': self.is_revoked, 'reactions': list(counts.values()),
            'starred': self.is_starred, 'pinned': self.is_pinned, 'remote': bool(self.remote_message_id),
            'user': self.user_id.name or '', 'editable': self._can_edit(),
            'location': {'lat': self.latitude, 'lng': self.longitude} if self.message_type == 'location' else False,
            'attachments': [{'id': a.id, 'name': a.name, 'mimetype': a.mimetype or '', 'size': a.file_size,
                             'image': (a.mimetype or '').startswith('image/')}
                            for a in self.attachment_ids],
        }

    def _set_ack(self, status):
        """Never downgrade a delivery status (events may arrive out of order)."""
        for message in self:
            if status == 'failed' and message.delivery_status in ('delivered', 'read'):
                continue
            if status == 'failed' or ACK_RANK.get(status, 0) > ACK_RANK.get(message.delivery_status, 0):
                message.sudo().write({'delivery_status': status})
                message._notify_bus()

    def _deliver(self, do_send):
        """Run the WAHA call and record the outcome (ambiguous network errors are never retried)."""
        self.ensure_one()
        try:
            response = do_send()
        except WahaError as e:
            status = 'unknown' if e.kind in ('network', 'response') else 'failed'
            self.write({'delivery_status': status, 'failure_reason': str(e)})
            return self
        self._apply_send_response(response)
        return self

    def _apply_send_response(self, response):
        """Attach the WAHA id to a message we just sent; merge with a webhook-created twin."""
        self.ensure_one()
        remote_id = extract_message_id(response)
        if not remote_id:
            self.write({'delivery_status': 'unknown', 'failure_reason': self.env._("WAHA did not return a message id.")})
            return
        twin = self.search([('session_id', '=', self.session_id.id), ('remote_message_id', '=', remote_id),
                            ('id', '!=', self.id)], limit=1)
        if twin:
            # the webhook won the race: keep our row (it has the sender/token), drop the twin
            status = twin.delivery_status
            twin.unlink()
            self.write({'remote_message_id': remote_id, 'delivery_status': status or 'sent'})
        else:
            self.write({'remote_message_id': remote_id, 'delivery_status': 'sent', 'failure_reason': False})


def ts_to_datetime(ts):
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return fields.Datetime.now()

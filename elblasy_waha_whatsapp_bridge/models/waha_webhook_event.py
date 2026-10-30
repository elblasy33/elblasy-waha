import logging

from odoo import fields, models
from odoo.tools import plaintext2html

from odoo.addons.elblasy_waha_connector.models.waha_client import WahaError

from ..tools import transport

_logger = logging.getLogger(__name__)


class WahaWebhookEvent(models.Model):
    _inherit = 'elblasy.waha.webhook.event'

    bridge_message_ref = fields.Char(index='btree_not_null', help="WhatsApp message id handled by the native app.")

    def _dispatch(self, data):
        session = self.session_id.sudo()
        account = session._get_whatsapp_account()
        etype = data.get('event')
        payload = data.get('payload')
        if not account or not isinstance(payload, dict) or etype not in ('message.any', 'message.reaction', 'message.ack'):
            return super()._dispatch(data)
        # group chats are handled by the group feature, everything else by the native WhatsApp app
        if str(payload.get('from') or '').endswith('@g.us') or str(payload.get('to') or '').endswith('@g.us'):
            return super()._dispatch(data)
        # session bound to the native WhatsApp app: it owns messages, the custom inbox is bypassed
        if etype == 'message.ack':
            return self._bridge_ack(payload)
        if not payload.get('id'):
            return 'ignored'
        if payload.get('fromMe'):
            if etype != 'message.any':
                return 'ignored'
            return self._bridge_outgoing(session, account, payload)
        if etype == 'message.any':
            # a webhook and a manual sync can both deliver the same message: handle it once
            if self.search_count([('session_id', '=', session.id), ('bridge_message_ref', '=', payload['id']),
                                  ('state', '=', 'done'), ('id', '!=', self.id)]):
                return 'ignored'
        client = session.server_id._get_client()
        resolve = lambda lid: client.lid_to_phone(session.session_name, lid)  # noqa: E731
        if etype == 'message.any':
            value = transport.meta_value_from_message(account, payload, resolve)
        else:
            value = transport.meta_value_from_reaction(account, payload, resolve)
        if not value:
            return 'ignored'
        account._process_messages(value)
        if etype == 'message.any':
            self.bridge_message_ref = payload['id']
        return 'done'

    def _bridge_ack(self, payload):
        status = transport.ACK_STATUS.get(payload.get('ack'))
        if payload.get('ack') == -1:
            status = 'failed'
        if not payload.get('id') or not status:
            return 'ignored'
        entry = {'id': payload['id'], 'status': status}
        if status == 'failed':
            entry['errors'] = [{'code': 0, 'title': "Delivery failed"}]
        found = self.env['whatsapp.message']._process_statuses({'statuses': [entry]})
        if not found:
            raise LookupError("message not yet known")  # ack may overtake the send response: retried by cron
        return 'done'

    ECHO_WINDOW_MINUTES = 2

    def _bridge_outgoing(self, session, account, payload):
        """A message sent from the phone, WhatsApp Web or the inbox: show it in the Discuss chat, like WhatsApp
        shows it on every linked device. Posted as a plain comment so Odoo never sends it a second time."""
        WaMessage = self.env['whatsapp.message'].sudo()
        if WaMessage.search_count([('msg_uid', '=', payload['id'])]):
            return 'ignored'  # sent from Odoo itself, or already shown
        client = session.server_id._get_client()
        resolve = lambda lid: client.lid_to_phone(session.session_name, lid)  # noqa: E731
        echo = dict(payload, **{'from': payload.get('to'), 'fromMe': False})
        value = transport.meta_value_from_message(account, echo, resolve)
        if not value:
            return 'ignored'
        message = value['messages'][0]
        number = message.get('from')
        recent = fields.Datetime.subtract(fields.Datetime.now(), minutes=self.ECHO_WINDOW_MINUTES)
        if number and WaMessage.search_count([
                ('wa_account_id', '=', account.id), ('message_type', '=', 'outbound'), ('msg_uid', '=', False),
                ('mobile_number_formatted', 'like', number[-9:]), ('create_date', '>=', recent)]):
            return 'ignored'  # sent from Discuss a moment ago: its WhatsApp id is being saved right now
        channel = account._find_active_channel_from_whatsapp_message_values(
            message, value.get('contacts'), create_if_not_found=True)
        if not channel:
            return 'ignored'
        body = payload.get('body') or ''
        attachments = []
        media = payload.get('media') if isinstance(payload.get('media'), dict) else {}
        if payload.get('hasMedia') and media.get('url'):
            try:
                raw = client.download(media['url'], max_bytes=20 * 1024 * 1024)
                kind = message.get('type', 'document')
                attachments.append((media.get('filename') or kind, raw))
            except WahaError as e:
                _logger.info("Media of outgoing message %s not copied to Discuss: %s", payload['id'][-6:], e)
        if transport.looks_like_thumbnail(body):
            body = ''  # a picture preview, never text
        if payload.get('hasMedia') and not attachments and not body:
            body = self.env._("📎 File sent from WhatsApp (open the phone to view it)")
        if not body and not attachments:
            return 'ignored'
        posted = channel.sudo().message_post(
            body=plaintext2html(body) if body else '', attachments=attachments, message_type='comment',
            subtype_xmlid='mail.mt_comment', author_id=session.company_id.partner_id.id)
        WaMessage.create({
            'mail_message_id': posted.id, 'message_type': 'outbound', 'msg_uid': payload['id'],
            'wa_account_id': account.id, 'mobile_number': number or '',
            'state': transport.ACK_STATUS.get(payload.get('ack'), 'sent'),
        })
        self.bridge_message_ref = payload['id']
        return 'done'

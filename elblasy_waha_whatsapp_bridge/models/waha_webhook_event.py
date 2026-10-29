from odoo import fields, models

from ..tools import transport


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
        if payload.get('fromMe') or not payload.get('id'):
            return 'ignored'
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

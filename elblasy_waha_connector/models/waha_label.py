import logging

from odoo import api, fields, models

from .waha_client import WahaError

_logger = logging.getLogger(__name__)


def _chat_id_of(item):
    """WAHA may list chats as strings or as objects with a (serialized) id."""
    value = item.get('id') if isinstance(item, dict) else item
    return value.get('_serialized') if isinstance(value, dict) else value


class WahaLabel(models.Model):
    """WhatsApp (Business) labels of a session, mirrored read/write with the phone."""
    _name = 'elblasy.waha.label'
    _description = "WhatsApp Label"
    _order = 'name'

    name = fields.Char(required=True)
    session_id = fields.Many2one('elblasy.waha.session', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='session_id.company_id', store=True, index=True)
    label_id = fields.Char("WhatsApp ID", required=True, readonly=True, index=True)
    color_hex = fields.Char(default='#8e8e8e')

    _label_uniq = models.Constraint('UNIQUE (session_id, label_id)', "This label already exists for the session.")

    @api.model
    def _upsert(self, session, data):
        label_id = str(data.get('id') or '')
        if not label_id:
            return self.browse()
        vals = {'name': data.get('name') or label_id, 'color_hex': data.get('colorHex') or '#8e8e8e'}
        label = self.sudo().search([('session_id', '=', session.id), ('label_id', '=', label_id)], limit=1)
        if label:
            label.write(vals)
            return label
        return self.sudo().create({**vals, 'session_id': session.id, 'label_id': label_id})

    @api.model
    def _sync_session(self, session):
        """Labels and the chats they are attached to. Returns the number of labels."""
        session.ensure_one()
        client = session.server_id._get_client()
        try:
            remote = client.get(f'/api/{session.session_name}/labels')
            remote = remote if isinstance(remote, list) else []
            labels = self.browse()
            chats_by_label = {}
            for item in remote:
                label = self._upsert(session, item)
                if label:
                    labels |= label
                    listed = client.get(f'/api/{session.session_name}/labels/{label.label_id}/chats')
                    chats_by_label[label.id] = {c for c in (_chat_id_of(i) for i in (listed if isinstance(listed, list) else [])) if c}
        except WahaError as e:
            session._fail(e)
        self.sudo().search([('session_id', '=', session.id), ('id', 'not in', labels.ids)]).unlink()
        Conversation = self.env['elblasy.waha.conversation'].sudo()
        for conv in Conversation.search([('session_id', '=', session.id)]):
            wanted = labels.filtered(lambda l: conv.remote_chat_id in chats_by_label.get(l.id, ()))
            if set(wanted.ids) != set(conv.label_ids.ids):
                conv.label_ids = [(6, 0, wanted.ids)]
        return len(labels)

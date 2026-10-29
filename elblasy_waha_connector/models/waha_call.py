from odoo import api, fields, models


class WahaCall(models.Model):
    """Calls received on a session (WhatsApp calls cannot be answered through the API)."""
    _name = 'elblasy.waha.call'
    _description = "WhatsApp Call"
    _order = 'id desc'

    session_id = fields.Many2one('elblasy.waha.session', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='session_id.company_id', store=True, index=True)
    chat_id = fields.Char("Caller", required=True, index=True)
    partner_id = fields.Many2one('res.partner', compute='_compute_partner', store=True)
    remote_call_id = fields.Char(index=True)
    is_video = fields.Boolean()
    date = fields.Datetime(default=fields.Datetime.now, index=True)
    rejected = fields.Boolean()
    replied = fields.Boolean()

    @api.depends('chat_id')
    def _compute_partner(self):
        Conversation = self.env['elblasy.waha.conversation'].sudo()
        for call in self:
            conv = Conversation.search([('remote_chat_id', '=', call.chat_id)], limit=1)
            call.partner_id = conv.partner_id

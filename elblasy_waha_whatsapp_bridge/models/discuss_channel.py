from odoo import api, fields, models


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    @api.depends('last_wa_mail_message_id', 'wa_account_id.waha_session_id')
    def _compute_whatsapp_channel_valid_until(self):
        """Meta's 24h customer-care window does not exist with WAHA: such channels stay open for replies."""
        super()._compute_whatsapp_channel_valid_until()
        far = fields.Datetime.add(fields.Datetime.now(), years=10)
        for channel in self.filtered(lambda c: c.channel_type == 'whatsapp' and c.wa_account_id.sudo().waha_session_id):
            channel.whatsapp_channel_valid_until = far

    def _waha_open_action(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_url', 'target': 'self',
                'url': f'/odoo/action-mail.action_discuss?active_id=discuss.channel_{self.id}'}

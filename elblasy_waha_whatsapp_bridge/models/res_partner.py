from odoo import models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    def action_waha_discuss_chat(self):
        """Open the native Discuss WhatsApp chat with this contact (asks for the account when needed)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': self.env._("New WhatsApp chat"),
            'res_model': 'elblasy.waha.new.chat', 'view_mode': 'form', 'target': 'new',
            'context': {'active_model': self._name, 'active_id': self.id},
        }

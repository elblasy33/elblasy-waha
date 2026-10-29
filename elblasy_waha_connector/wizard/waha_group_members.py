from odoo import fields, models


class WahaGroupMembers(models.TransientModel):
    _name = 'elblasy.waha.group.members'
    _description = "Add members to a WhatsApp group"

    group_id = fields.Many2one('elblasy.waha.group', required=True)
    partner_ids = fields.Many2many('res.partner', string="Contacts", required=True)

    def action_add(self):
        self.ensure_one()
        self.group_id.add_participants(self.partner_ids)
        return {'type': 'ir.actions.act_window_close'}

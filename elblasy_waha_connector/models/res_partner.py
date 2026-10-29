import re

from odoo import api, fields, models


def digits_only(value):
    return re.sub(r'\D', '', value or '')


class ResPartner(models.Model):
    _inherit = 'res.partner'

    waha_phone_digits = fields.Char(compute='_compute_waha_phone_digits', store=True, index='btree')
    waha_conversation_ids = fields.One2many('elblasy.waha.conversation', 'partner_id')
    waha_conversation_count = fields.Integer(compute='_compute_waha_conversation_count')
    waha_group_ids = fields.Many2many(
        'elblasy.waha.group', 'res_partner_waha_group_rel', 'partner_id', 'group_id',
        string="WhatsApp Groups", help="Groups that receive the documents sent to this contact.")

    @api.depends('phone')
    def _compute_waha_phone_digits(self):
        for partner in self:
            partner.waha_phone_digits = digits_only(partner.phone) or False

    def _compute_waha_conversation_count(self):
        data = self.env['elblasy.waha.conversation']._read_group(
            [('partner_id', 'in', self.ids)], ['partner_id'], ['__count'])
        counts = {partner.id: count for partner, count in data}
        for partner in self:
            partner.waha_conversation_count = counts.get(partner.id, 0)

    def action_open_waha_conversations(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': self.env._("WhatsApp Conversations"),
            'res_model': 'elblasy.waha.conversation', 'view_mode': 'list,form',
            'domain': [('partner_id', '=', self.id)],
        }

    def action_send_whatsapp(self):
        """Instant message from the contact form."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': self.env._("Send WhatsApp"),
            'res_model': 'elblasy.waha.group.send', 'view_mode': 'form', 'target': 'new',
            'context': {'active_model': self._name, 'active_id': self.id},
        }

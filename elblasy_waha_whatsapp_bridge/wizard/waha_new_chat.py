from odoo import api, fields, models
from odoo.exceptions import UserError


class WahaNewChat(models.TransientModel):
    _name = 'elblasy.waha.new.chat'
    _description = "Start a WhatsApp chat in Discuss"

    account_id = fields.Many2one(
        'whatsapp.account', string="WhatsApp account", required=True,
        domain="[('waha_session_id', '!=', False)]")
    partner_id = fields.Many2one('res.partner', string="Contact")
    phone = fields.Char("Phone", help="International format, e.g. +20 100 123 4567. Filled from the contact.")
    message = fields.Text("First message")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ctx = self.env.context
        record = self.env[ctx['active_model']].browse(ctx['active_id']).exists() \
            if ctx.get('active_model') and ctx.get('active_id') else None
        if record:
            partner, phone = self.env['elblasy.waha.group.post']._record_targets(record)
            res.update(partner_id=partner.id, phone=phone)
        account = self.env['whatsapp.account'].search([('waha_session_id', '!=', False)], limit=1)
        res.setdefault('account_id', account.id)
        return res

    @api.onchange('partner_id')
    def _onchange_partner_id(self):
        if self.partner_id.phone:
            self.phone = self.partner_id.phone

    def action_start(self):
        self.ensure_one()
        if not (self.phone or self.partner_id.phone):
            raise UserError(self.env._("Choose a contact with a phone number or type a number."))
        channel = self.account_id._waha_start_chat(
            number=self.phone, partner=self.partner_id, first_message=self.message)
        return channel._waha_open_action()

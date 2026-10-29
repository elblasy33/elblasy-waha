import re

from odoo import fields, models
from odoo.exceptions import UserError

from odoo.addons.elblasy_waha_connector.models.waha_client import WahaError
from odoo.addons.elblasy_waha_connector.models.waha_group import _serialized


class WahaGroupCreate(models.TransientModel):
    _name = 'elblasy.waha.group.create'
    _description = "Create a WhatsApp group"

    session_id = fields.Many2one('elblasy.waha.session', required=True, domain="[('status', '=', 'WORKING')]")
    name = fields.Char("Group name", required=True)
    partner_ids = fields.Many2many('res.partner', string="Members", help="Contacts with a phone number")
    link_to_partners = fields.Boolean("Link to these contacts", default=True,
                                      help="Add the group to each contact so documents also go to it.")

    def action_create(self):
        self.ensure_one()
        self.session_id._check_manager()
        participants = []
        for partner in self.partner_ids:
            digits = re.sub(r'\D', '', partner.phone or '')
            digits = digits[2:] if digits.startswith('00') else digits
            if len(digits) < 7:
                raise UserError(self.env._("%s has no valid phone number.", partner.name))
            participants.append({'id': f"{digits}@c.us"})
        client = self.session_id.server_id._get_client()
        try:
            data = client.post(f'/api/{self.session_id.session_name}/groups',
                               {'name': self.name, 'participants': participants})
        except WahaError as e:
            self.session_id._fail(e)
        chat_id = False
        for key in ('id', 'gid'):
            value = _serialized((data or {}).get(key)) if isinstance(data, dict) else None
            if isinstance(value, str) and value.endswith('@g.us'):
                chat_id = value
                break
        Group = self.env['elblasy.waha.group'].sudo()
        group = Group.search([('session_id', '=', self.session_id.id), ('chat_id', '=', chat_id)], limit=1) \
            if chat_id else Group
        if chat_id and not group:
            group = Group.create({'name': self.name, 'session_id': self.session_id.id, 'chat_id': chat_id})
        if not group:
            raise UserError(self.env._(
                "The group was created on WhatsApp but WAHA did not return its id. Use Sync Groups to import it."))
        group.write({'allow_send': True, 'participants_count': len(participants) + 1})
        if self.link_to_partners:
            self.partner_ids.sudo().write({'waha_group_ids': [(4, group.id)]})  # WhatsApp managers may link groups
        return {'type': 'ir.actions.act_window', 'res_model': 'elblasy.waha.group', 'res_id': group.id,
                'view_mode': 'form', 'views': [(False, 'form')]}

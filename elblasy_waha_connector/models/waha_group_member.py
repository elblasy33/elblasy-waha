import re

from odoo import api, fields, models


class WahaGroupMember(models.Model):
    _name = 'elblasy.waha.group.member'
    _description = "WhatsApp Group Member"
    _order = 'role desc, wa_id'

    group_id = fields.Many2one('elblasy.waha.group', required=True, ondelete='cascade', index=True)
    session_id = fields.Many2one(related='group_id.session_id', store=True)
    company_id = fields.Many2one(related='group_id.company_id', store=True, index=True)
    wa_id = fields.Char("WhatsApp ID", required=True, readonly=True)
    phone = fields.Char(compute='_compute_phone', store=True)
    role = fields.Selection([('participant', "Member"), ('admin', "Admin"), ('superadmin', "Owner")],
                            default='participant', readonly=True)
    partner_id = fields.Many2one('res.partner', compute='_compute_partner', store=True, readonly=False)

    _member_uniq = models.Constraint('UNIQUE (group_id, wa_id)', "Member already in the group.")

    @api.depends('wa_id')
    def _compute_phone(self):
        for member in self:
            local, _sep, suffix = (member.wa_id or '').rpartition('@')
            member.phone = re.sub(r'\D', '', local) if suffix in ('c.us', 's.whatsapp.net') else False

    @api.depends('phone')
    def _compute_partner(self):
        Partner = self.env['res.partner'].sudo()
        for member in self:
            partner = Partner
            if member.phone and len(member.phone) >= 7:
                tail = member.phone[-9:]
                matches = Partner.search([('waha_phone_digits', 'like', tail)]).filtered(
                    lambda p: p.waha_phone_digits.endswith(tail))
                partner = matches if len(matches) == 1 else Partner
            member.partner_id = partner

    def _call(self, path):
        self.ensure_one()
        group = self.group_id
        group._manage('post', path, group._participants_payload([self.wa_id]))
        return group.action_sync_members()

    def action_remove(self):
        return self._call('/participants/remove')

    def action_promote(self):
        return self._call('/admin/promote')

    def action_demote(self):
        return self._call('/admin/demote')

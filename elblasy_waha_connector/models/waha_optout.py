import re

from odoo import api, fields, models

DEFAULT_OPTOUT = "stop,unsubscribe,إيقاف,ايقاف,الغاء الاشتراك,إلغاء الاشتراك"
DEFAULT_OPTIN = "start,subscribe,اشتراك"
DEFAULT_REPLY = "You have been unsubscribed from automatic messages. Reply START to subscribe again."


class WahaOptout(models.Model):
    """Numbers that asked to stop receiving automatic messages (sent through the wizard or automation)."""
    _name = 'elblasy.waha.optout'
    _description = "WhatsApp Opt-out"
    _order = 'id desc'

    phone = fields.Char("Phone (digits)", required=True, index=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company, index=True)
    date = fields.Datetime(default=fields.Datetime.now, readonly=True)
    source = fields.Selection([('keyword', "Customer message"), ('manual', "Manual")], default='manual')

    _phone_uniq = models.Constraint('UNIQUE (company_id, phone)', "This number is already opted out.")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals['phone'] = re.sub(r'\D', '', vals.get('phone') or '')
        return super().create(vals_list)

    @api.model
    def _param(self, key, default):
        return self.env['ir.config_parameter'].sudo().get_str(f'elblasy_waha.{key}', default)

    @api.model
    def _keywords(self, kind):
        raw = self._param('optout_keywords' if kind == 'optout' else 'optin_keywords',
                          DEFAULT_OPTOUT if kind == 'optout' else DEFAULT_OPTIN)
        return {word.strip().casefold() for word in raw.split(',') if word.strip()}

    @api.model
    def _reply(self, key):
        return self._param(key, DEFAULT_REPLY)

    @api.model
    def _find(self, company, digits):
        digits = re.sub(r'\D', '', digits or '')
        if len(digits) < 7:
            return self.browse()
        return self.sudo().search([('company_id', '=', company.id), ('phone', '=like', '%' + digits[-9:])]).filtered(
            lambda o: o.phone.endswith(digits[-9:]))

    @api.model
    def _is_opted_out(self, company, digits):
        return bool(self._find(company, digits))

    @api.model
    def _opt_out(self, company, digits):
        if not self._find(company, digits):
            self.sudo().create({'phone': digits, 'company_id': company.id, 'source': 'keyword'})

    @api.model
    def _opt_in(self, company, digits):
        self._find(company, digits).unlink()

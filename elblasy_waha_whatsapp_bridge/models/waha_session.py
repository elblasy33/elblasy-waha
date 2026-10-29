import secrets

from odoo import fields, models
from odoo.exceptions import AccessError, UserError

ADMIN = 'whatsapp.group_whatsapp_admin'


class WahaSession(models.Model):
    _inherit = 'elblasy.waha.session'

    whatsapp_account_ids = fields.One2many('whatsapp.account', 'waha_session_id')
    odoo_whatsapp_enabled = fields.Boolean("Odoo WhatsApp via WAHA", compute='_compute_odoo_whatsapp_enabled')

    def _compute_odoo_whatsapp_enabled(self):
        for session in self:
            session.odoo_whatsapp_enabled = bool(session.sudo().whatsapp_account_ids)

    def _get_whatsapp_account(self):
        self.ensure_one()
        return self.sudo().whatsapp_account_ids[:1]

    def _webhook_config(self):
        config = super()._webhook_config()
        if self._get_whatsapp_account():
            events = config['webhooks'][0]['events']
            if 'message.reaction' not in events:
                events.append('message.reaction')
        return config

    def action_enable_odoo_whatsapp(self):
        """Create the native whatsapp.account bound to this session (idempotent)."""
        self.ensure_one()
        if not self.env.user.has_group(ADMIN):
            raise AccessError(self.env._("Only WhatsApp administrators can enable this."))
        account = self._get_whatsapp_account()
        if not account:
            account = self.env['whatsapp.account'].sudo().create({
                'name': f"{self.name} (WAHA)",
                'waha_session_id': self.id,
                # Meta credentials are mandatory in the native model but unused with WAHA
                'app_uid': f"waha-{self.id}",
                'account_uid': f"waha-{self.id}",
                'phone_uid': f"waha-{self.id}",
                'app_secret': secrets.token_urlsafe(24),
                'token': secrets.token_urlsafe(24),
                'phone_number': self.phone_number or False,
                'notify_user_ids': [(6, 0, self.env.user.ids)],
                'allowed_company_ids': [(6, 0, self.company_id.ids)],
            })
        # make WAHA also send message.reaction events
        if self.status != 'STOPPED':
            try:
                self.action_connect()
            except UserError:
                pass
        return self.action_open_whatsapp_account()

    def action_disable_odoo_whatsapp(self):
        self.ensure_one()
        if not self.env.user.has_group(ADMIN):
            raise AccessError(self.env._("Only WhatsApp administrators can disable this."))
        self.sudo().whatsapp_account_ids.write({'active': False, 'waha_session_id': False})
        return True

    def action_open_whatsapp_account(self):
        self.ensure_one()
        account = self._get_whatsapp_account()
        return {'type': 'ir.actions.act_window', 'res_model': 'whatsapp.account',
                'res_id': account.id, 'view_mode': 'form', 'views': [(False, 'form')]}

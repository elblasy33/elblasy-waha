import secrets

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .waha_client import WahaClient, WahaError

MANAGER = 'elblasy_waha_connector.group_waha_manager'
ADMIN = 'elblasy_waha_connector.group_waha_admin'


class WahaServer(models.Model):
    _name = 'elblasy.waha.server'
    _description = "WAHA Server"
    _order = 'name'

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company, index=True)
    base_url = fields.Char("WAHA Base URL", required=True, help="e.g. https://waha.example.com")
    # Secrets live in the database in clear text and are only readable by managers
    # (field-level groups, enforced by the ORM on every read/write/RPC).
    api_key = fields.Char("API Key", groups=MANAGER, copy=False)
    webhook_secret = fields.Char("Webhook HMAC Secret", groups=MANAGER, copy=False,
                                 default=lambda s: secrets.token_urlsafe(32))
    has_api_key = fields.Boolean(compute='_compute_has_api_key')
    timeout_connect = fields.Integer("Connect Timeout (s)", default=5)
    timeout_read = fields.Integer("Response Timeout (s)", default=30)
    verify_ssl = fields.Boolean("Verify HTTPS Certificate", default=True)
    webhook_base_url = fields.Char(
        "Odoo Public URL", help="URL WAHA uses to reach this Odoo. Defaults to web.base.url.")
    use_hmac = fields.Boolean("Verify Webhook HMAC", default=True)
    last_test_date = fields.Datetime(readonly=True)
    last_test_result = fields.Char(readonly=True)
    session_ids = fields.One2many('elblasy.waha.session', 'server_id')

    _check_timeouts = models.Constraint(
        'CHECK (timeout_connect > 0 AND timeout_read > 0)', "Timeouts must be positive.")

    def _compute_has_api_key(self):
        for server in self:
            server.has_api_key = bool(server.sudo().api_key)

    def _get_client(self):
        """Build a client. Must be called on a (sudo) record, secrets are read here only."""
        self.ensure_one()
        server = self.sudo()
        return WahaClient(server.base_url, server.api_key, server.timeout_connect,
                          server.timeout_read, server.verify_ssl)

    def _get_webhook_base_url(self):
        self.ensure_one()
        base = self.webhook_base_url or self.env['ir.config_parameter'].sudo().get_str('web.base.url')
        return base.rstrip('/')

    @api.constrains('base_url')
    def _check_base_url(self):
        for server in self:
            if not (server.base_url or '').lower().startswith(('http://', 'https://')):
                raise UserError(self.env._("The WAHA Base URL must start with http:// or https://."))

    def _check_manager(self):
        if not self.env.user.has_group(MANAGER):
            raise AccessError(self.env._("Only WhatsApp managers can do this."))

    def action_test_connection(self):
        self.ensure_one()
        self._check_manager()
        try:
            sessions = self._get_client().list_sessions()
            result = self.env._("Connection OK (%s session(s) on server).", len(sessions) if isinstance(sessions, list) else 0)
            ntype = 'success'
        except WahaError as e:
            labels = {
                'network': self.env._("Network error"),
                'auth': self.env._("Authentication error"),
                'not_found': self.env._("Unexpected response"),
                'http': self.env._("Server error"),
                'response': self.env._("Unexpected response"),
            }
            result = f"{labels.get(e.kind, e.kind)}: {e}"
            ntype = 'danger'
        self.sudo().write({'last_test_date': fields.Datetime.now(), 'last_test_result': result})
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': self.env._("WAHA connection test"), 'message': result,
                       'type': ntype, 'sticky': ntype == 'danger'},
        }

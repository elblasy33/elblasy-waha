from datetime import timedelta

from odoo import fields, models


class IrActionsServer(models.Model):
    _inherit = 'ir.actions.server'

    state = fields.Selection(selection_add=[('waha_send', "Send WhatsApp (WAHA)"), ('followers',)],
                             ondelete={'waha_send': 'cascade'})
    waha_session_id = fields.Many2one('elblasy.waha.session', string="WhatsApp Session", ondelete='restrict')
    waha_body = fields.Text(
        "WhatsApp Message",
        help="Placeholders: {{ object.name }}, {{ object.partner_id.name }}, {{ object.amount_total }}...")
    waha_report_id = fields.Many2one(
        'ir.actions.report', string="Attach report", ondelete='set null',
        domain="[('model', '=', model_name), ('report_type', '=', 'qweb-pdf')]")
    waha_send_customer = fields.Boolean("Send to the customer's number", default=True)
    waha_use_partner_groups = fields.Boolean("Send to the customer's groups", default=True)
    waha_group_ids = fields.Many2many(
        'elblasy.waha.group', 'ir_actions_server_waha_group_rel', 'action_id', 'group_id',
        string="Also send to groups", domain="[('allow_send', '=', True)]")
    waha_once_per_record = fields.Boolean("Once per record", default=True)
    waha_delay_minutes = fields.Integer("Delay (minutes)", default=0)
    waha_user_id = fields.Many2one(
        'res.users', string="Send as", default=lambda s: s.env.user,
        help="Permissions of this user apply when the message is built and sent.")

    def _run_action_waha_send_multi(self, eval_context=None):
        if self._is_recompute():
            return False
        records = (eval_context or {}).get('records') or (eval_context or {}).get('record')
        if not records:
            return False
        Queue = self.env['elblasy.waha.queue']
        for record in records:
            Queue._enqueue(self, record)
        return False

    def _waha_scheduled_for(self):
        self.ensure_one()
        return fields.Datetime.now() + timedelta(minutes=max(self.waha_delay_minutes, 0))

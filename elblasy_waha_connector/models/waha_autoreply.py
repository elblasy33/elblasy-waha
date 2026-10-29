from datetime import timedelta

import pytz

from odoo import api, fields, models


class WahaAutoreply(models.Model):
    _name = 'elblasy.waha.autoreply'
    _description = "WhatsApp Auto-reply Rule"
    _order = 'sequence, id'

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    session_id = fields.Many2one('elblasy.waha.session', help="Leave empty to apply to every session.")
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company, index=True)
    trigger = fields.Selection(
        [('first', "First message of a conversation"), ('keyword', "Message contains a keyword"),
         ('outside_hours', "Outside business hours")], required=True, default='first')
    keywords = fields.Text(help="One per line or separated by commas (case-insensitive).")
    work_from = fields.Float("Work from", default=9.0)
    work_to = fields.Float("Work to", default=17.0)
    work_days = fields.Char("Work days", default='0,1,2,3,4', help="0 = Monday ... 6 = Sunday, separated by commas.")
    tz = fields.Selection(lambda self: [(t, t) for t in pytz.all_timezones], default='UTC', string="Time zone")
    reply_text = fields.Text("Reply", required=True, help="{name} is replaced by the customer's name.")
    cooldown_minutes = fields.Integer(
        "Do not reply again within (min)", default=60,
        help="Per conversation, shared by all rules: prevents answering every single message.")

    def _render(self, conversation):
        self.ensure_one()
        return (self.reply_text or '').replace('{name}', conversation.name or '')

    def _in_work_hours(self, now_utc):
        self.ensure_one()
        local = pytz.utc.localize(now_utc).astimezone(pytz.timezone(self.tz or 'UTC'))
        days = {int(d) for d in (self.work_days or '').split(',') if d.strip().isdigit()}
        hour = local.hour + local.minute / 60
        return local.weekday() in days and self.work_from <= hour < self.work_to

    def _matches(self, conversation, message):
        self.ensure_one()
        if self.trigger == 'first':
            return self.env['elblasy.waha.message'].sudo().search_count(
                [('conversation_id', '=', conversation.id), ('direction', '=', 'in')]) == 1
        if self.trigger == 'keyword':
            words = {w.strip().casefold() for w in (self.keywords or '').replace('\n', ',').split(',') if w.strip()}
            body = (message.body or '').casefold()
            return any(word in body for word in words)
        return not self._in_work_hours(fields.Datetime.now())

    @api.model
    def _match(self, conversation, message):
        """First matching rule, honouring the per-conversation cooldown."""
        now = fields.Datetime.now()
        rules = self.search([('active', '=', True), ('company_id', 'in', [False, conversation.company_id.id]),
                             ('session_id', 'in', [False, conversation.session_id.id])])
        for rule in rules:
            last = conversation.last_autoreply_date
            if last and now - last < timedelta(minutes=rule.cooldown_minutes):
                continue
            if rule._matches(conversation, message):
                return rule
        return self.browse()

import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.fields import Domain
from odoo.tools.safe_eval import safe_eval

_logger = logging.getLogger(__name__)

# Suggested documents, created when their app is installed (any other model can be added by hand)
DEFAULT_TRACKED = [
    # name, model, date field, partner field, amount field, domain
    ("Confirmed sales orders", 'sale.order', 'date_order', 'partner_id', 'amount_total', "[('state', '=', 'sale')]"),
    ("Customer invoices", 'account.move', 'invoice_date', 'partner_id', 'amount_total_signed',
     "[('move_type', '=', 'out_invoice'), ('state', '=', 'posted')]"),
    ("Leads & opportunities", 'crm.lead', 'create_date', 'partner_id', 'expected_revenue', "[]"),
    ("POS orders", 'pos.order', 'date_order', 'partner_id', 'amount_total', "[('state', 'in', ('paid', 'done'))]"),
    ("Helpdesk tickets", 'helpdesk.ticket', 'create_date', 'partner_id', False, "[]"),
    ("Tasks", 'project.task', 'create_date', 'partner_id', False, "[]"),
]


class WahaTrackedModel(models.Model):
    """A kind of business document measured against WhatsApp activity (quotes, invoices, tickets, any model)."""
    _name = 'elblasy.waha.tracked.model'
    _description = "WhatsApp Tracked Document"
    _order = 'sequence, id'

    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    model_id = fields.Many2one('ir.model', "Document", required=True, ondelete='cascade',
                               domain=[('transient', '=', False), ('abstract', '=', False)])
    model = fields.Char(related='model_id.model', store=True)
    name = fields.Char(compute='_compute_name', store=True, readonly=False, required=True, precompute=True)
    date_field_id = fields.Many2one(
        'ir.model.fields', "Date", required=True, ondelete='cascade',
        domain="[('model_id', '=', model_id), ('ttype', 'in', ('date', 'datetime')), ('store', '=', True)]")
    partner_field_id = fields.Many2one(
        'ir.model.fields', "Customer", required=True, ondelete='cascade',
        domain="[('model_id', '=', model_id), ('ttype', '=', 'many2one'), ('relation', '=', 'res.partner'), ('store', '=', True)]")
    amount_field_id = fields.Many2one(
        'ir.model.fields', "Amount", ondelete='set null',
        domain="[('model_id', '=', model_id), ('ttype', 'in', ('monetary', 'float', 'integer')), ('store', '=', True)]",
        help="Optional. Amounts are added as stored, without currency conversion.")
    domain = fields.Char("Only documents matching", default="[]")
    window_days = fields.Integer(
        "WhatsApp window (days)", default=30,
        help="A document counts as coming from WhatsApp when the customer exchanged messages in the days before it.")

    @api.depends('model_id')
    def _compute_name(self):
        for tracked in self:
            tracked.name = tracked.name or tracked.model_id.name

    @api.constrains('model_id', 'date_field_id', 'partner_field_id', 'amount_field_id', 'domain', 'window_days')
    def _check_fields(self):
        for tracked in self:
            for field in tracked.date_field_id | tracked.partner_field_id | tracked.amount_field_id:
                if field.model_id != tracked.model_id or not field.store:
                    raise ValidationError(self.env._("%s must be a stored field of %s.",
                                                     field.field_description, tracked.model_id.name))
            if tracked.window_days < 1:
                raise ValidationError(self.env._("The WhatsApp window must be at least one day."))
            tracked._parsed_domain()

    def _parsed_domain(self):
        try:
            domain = safe_eval(self.domain or '[]')
            Domain(domain).validate(self.env[self.model])
        except Exception as e:  # noqa: BLE001 - any mistake in a hand-written domain is reported the same way
            raise ValidationError(self.env._("Invalid filter for %(doc)s: %(error)s", doc=self.name, error=e))
        return domain

    @api.model
    def _ensure_defaults(self):
        """Suggest the usual documents of the installed apps (once; users may archive or change them)."""
        Fields = self.env['ir.model.fields']
        for label, model, date_f, partner_f, amount_f, domain in DEFAULT_TRACKED:
            if model not in self.env:
                continue
            ir_model = self.env['ir.model']._get(model)
            existing = self.with_context(active_test=False).search([('model_id', '=', ir_model.id)])
            if existing:
                existing.filtered(lambda t: t.name == ir_model.name).name = label  # auto-named before
                continue
            field = {name: Fields._get(model, name) for name in (date_f, partner_f, amount_f) if name}
            if not field.get(date_f) or not field.get(partner_f):
                continue
            self.create({'name': label, 'model_id': ir_model.id, 'date_field_id': field[date_f].id,
                         'partner_field_id': field[partner_f].id,
                         'amount_field_id': field[amount_f].id if amount_f and field.get(amount_f) else False,
                         'domain': domain})

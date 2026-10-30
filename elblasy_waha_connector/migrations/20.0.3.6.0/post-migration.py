from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Suggest the usual business documents (sales orders, invoices, leads, POS orders, tickets...)."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['elblasy.waha.tracked.model']._ensure_defaults()

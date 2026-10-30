from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """The cron record is noupdate: give it its new role (webhook events are now processed there)."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    cron = env.ref('elblasy_waha_connector.cron_waha_retry_events', raise_if_not_found=False)
    if cron:
        cron.write({'name': "WAHA: process webhook events", 'interval_number': 5, 'interval_type': 'minutes'})

from odoo import SUPERUSER_ID, api

from odoo.addons.elblasy_waha_connector.hooks import ensure_action_menu_bindings, ensure_hr_integration


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    ensure_action_menu_bindings(env)
    ensure_hr_integration(env)  # also rewrites the old button (which called a Python method that no longer exists)

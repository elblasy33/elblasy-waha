import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.elblasy_waha_connector.hooks import ensure_hr_integration

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    # chats of the same person used to be split between its phone id and its hidden (LID) id
    merged = env['elblasy.waha.conversation']._merge_duplicates()
    _logger.info("WhatsApp: %s duplicate conversation(s) merged", merged)
    ensure_hr_integration(env)  # Odoo 20 icon on the employee button (Font Awesome is gone)

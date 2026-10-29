"""Merge of elblasy_waha_groups and elblasy_waha_automation into elblasy_waha_connector.

The models keep their technical names (elblasy.waha.*), so no table is touched: only the
ownership of the existing XML ids (models, fields, views, menus, security, cron...) moves
to this module, and the absorbed module records are removed from the Apps list.
"""
import logging

_logger = logging.getLogger(__name__)
ABSORBED = ('elblasy_waha_groups', 'elblasy_waha_automation')
TARGET = 'elblasy_waha_connector'


def absorb(cr, modules):
    cr.execute("SELECT 1 FROM ir_module_module WHERE name = ANY(%s)", [list(modules)])
    if not cr.rowcount:
        return
    # a record of the same name already owned by the target would violate the unique (module, name)
    cr.execute("""
        DELETE FROM ir_model_data old USING ir_model_data new
         WHERE old.module = ANY(%s) AND new.module = %s AND new.name = old.name
    """, [list(modules), TARGET])
    cr.execute("UPDATE ir_model_data SET module = %s WHERE module = ANY(%s)", [TARGET, list(modules)])
    _logger.info("elblasy merge: %s XML ids moved to %s", cr.rowcount, TARGET)
    cr.execute("DELETE FROM ir_module_module_dependency WHERE name = ANY(%s)", [list(modules)])
    cr.execute("DELETE FROM ir_module_module WHERE name = ANY(%s)", [list(modules)])


def migrate(cr, version):
    absorb(cr, ABSORBED)

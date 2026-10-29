"""Second step of the merge: the employee button (elblasy_waha_groups_hr) joins this module, and the
leftover (uninstalled) records of the absorbed modules disappear from the Apps list."""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    'elblasy_merge_2_0_0', pathlib.Path(__file__).parent.parent / '20.0.2.0.0' / 'pre-migration.py')
_merge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_merge)


def migrate(cr, version):
    _merge.absorb(cr, _merge.ABSORBED + ('elblasy_waha_groups_hr',))
    # these two records come from the former add-on and are not in a data file here: keep them
    # (the post-migration rewrites them) instead of letting the end-of-update clean-up delete them
    cr.execute("""UPDATE ir_model_data SET noupdate = TRUE
                    WHERE module = %s AND name IN ('hr_employee_view_form_waha', 'hr_employee_action_send_whatsapp')""",
               [_merge.TARGET])

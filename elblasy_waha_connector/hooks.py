"""Integrations with optional apps, created only when those apps are installed.

The window "Send to WhatsApp" is offered in the Action menu of common business documents, and the employee
form gets an instant-message button when ``hr`` is present: no extra module is needed for it.
"""
BINDABLE_MODELS = ['sale.order', 'account.move', 'stock.picking', 'purchase.order', 'crm.lead',
                   'res.partner', 'hr.employee', 'project.task', 'helpdesk.ticket', 'mrp.production', 'hr.expense']
HR_VIEW_XMLID = 'hr_employee_view_form_waha'
HR_VIEW_ARCH = """<data>
    <div name="button_box" position="inside">
        <button name="elblasy_waha_connector.waha_group_send_action" type="action" class="oe_stat_button"
                icon="fa-whatsapp" string="Send WhatsApp" groups="elblasy_waha_connector.group_waha_user"/>
    </div>
</data>"""


def ensure_action_menu_bindings(env):
    Action = env['ir.actions.act_window'].sudo()
    for model_name in BINDABLE_MODELS:
        if model_name not in env:
            continue
        model = env['ir.model']._get(model_name)
        if Action.search_count([('res_model', '=', 'elblasy.waha.group.send'), ('binding_model_id', '=', model.id)]):
            continue
        Action.create({
            'name': env._("Send to WhatsApp group"),
            'res_model': 'elblasy.waha.group.send',
            'view_mode': 'form',
            'target': 'new',
            'binding_model_id': model.id,
            'binding_view_types': 'form',
            'group_ids': [(4, env.ref('elblasy_waha_connector.group_waha_user').id)],
        })


def ensure_hr_integration(env):
    """Instant-message button on the employee form (a plain action button: no Python needed on hr.employee)."""
    parent = env.ref('hr.view_employee_form', raise_if_not_found=False) if 'hr.employee' in env else None
    if not parent:
        return
    View = env['ir.ui.view'].sudo()
    view = env.ref(f'elblasy_waha_connector.{HR_VIEW_XMLID}', raise_if_not_found=False)
    if view:
        view.sudo().write({'arch_base': HR_VIEW_ARCH, 'inherit_id': parent.id})
        return
    view = View.create({'name': 'hr.employee.form.waha', 'model': 'hr.employee', 'inherit_id': parent.id,
                        'arch_base': HR_VIEW_ARCH})
    env['ir.model.data'].sudo().create({
        'module': 'elblasy_waha_connector', 'name': HR_VIEW_XMLID, 'model': 'ir.ui.view',
        'res_id': view.id, 'noupdate': True})


def post_init_hook(env):
    ensure_action_menu_bindings(env)
    ensure_hr_integration(env)

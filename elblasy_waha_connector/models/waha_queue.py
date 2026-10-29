import logging
import time

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import config

_logger = logging.getLogger(__name__)
BATCH = 10  # items per cron run
PAUSE = 3  # seconds between two items: sending slowly protects the number


class WahaQueue(models.Model):
    _name = 'elblasy.waha.queue'
    _description = "WhatsApp Automation Queue"
    _order = 'id desc'

    action_id = fields.Many2one('ir.actions.server', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company, index=True)
    res_model = fields.Char(required=True)
    res_id = fields.Integer(required=True)
    record_name = fields.Char()
    scheduled_for = fields.Datetime(default=fields.Datetime.now, index=True)
    state = fields.Selection(
        [('pending', "Pending"), ('done', "Done"), ('failed', "Failed"), ('unknown', "Not confirmed"),
         ('skipped', "Skipped")], default='pending', index=True)
    attempts = fields.Integer(default=0)
    error = fields.Char()
    post_ids = fields.Many2many('elblasy.waha.group.post', string="Messages")

    @api.model
    def _enqueue(self, action, record):
        if action.waha_once_per_record and self.sudo().search_count([
                ('action_id', '=', action.id), ('res_model', '=', record._name), ('res_id', '=', record.id)]):
            return self.browse()
        return self.sudo().create({
            'action_id': action.id, 'res_model': record._name, 'res_id': record.id,
            'record_name': record.display_name, 'scheduled_for': action._waha_scheduled_for(),
        })

    # ------------------------------------------------------------------
    def _run_item(self):
        self.ensure_one()
        action = self.action_id.sudo()
        user = action.waha_user_id
        if not user or not action.waha_session_id:
            return self.write({'state': 'failed', 'error': "Action without sender or session."})
        env_user = self.env(user=user, su=False)
        record = env_user[self.res_model].browse(self.res_id).exists()
        if not record:
            return self.write({'state': 'skipped', 'error': "Record no longer exists."})
        Post = env_user['elblasy.waha.group.post']
        partner, phone = Post._record_targets(record)
        body = action.waha_body and env_user['mail.render.mixin']._render_template(
            action.waha_body, record._name, record.ids, engine='inline_template')[record.id] or ''
        attachments = env_user['ir.attachment']
        if action.waha_report_id:
            pdf, _fmt = env_user['ir.actions.report']._render_qweb_pdf(action.waha_report_id, [record.id])
            attachments = attachments.create({
                'name': f"{record.display_name or action.waha_report_id.name}.pdf", 'raw': pdf,
                'mimetype': 'application/pdf'})
        self.attempts += 1
        prefix = f"q{self.id}-{self.attempts}"
        posts = env_user['elblasy.waha.group.post']
        errors = []
        targets = []
        if action.waha_send_customer and phone:
            targets.append(('customer', lambda: Post.send_to_number(
                phone, action.waha_session_id, body, attachments, record._name, record.id, f"{prefix}-c",
                partner=partner)))
        groups = action.waha_group_ids
        if action.waha_use_partner_groups and partner:
            groups |= partner.waha_group_ids
        for group in groups.filtered(lambda g: g.allow_send and g.active):
            targets.append((group.name, lambda g=group: Post.send_to_group(
                g, body, attachments, record._name, record.id, f"{prefix}-{g.id}")))
        if not targets:
            return self.write({'state': 'skipped', 'error': "No phone number or group to send to."})
        for label, send in targets:
            try:
                posts |= send()
            except UserError as e:
                errors.append(f"{label}: {e}")
        states = set(posts.mapped('state'))
        state = 'failed' if errors or 'failed' in states else 'unknown' if 'unknown' in states else 'done'
        self.write({'state': state, 'error': '; '.join(errors)[:250] or False, 'post_ids': [(6, 0, posts.ids)]})

    def action_retry(self):
        # explicit user action; ambiguous ('unknown') items must be checked by a human first
        self.filtered(lambda i: i.state in ('failed', 'unknown')).write({'state': 'pending', 'error': False})

    def action_run_now(self):
        self.filtered(lambda i: i.state == 'pending')._run_batch(pause=False)

    def _run_batch(self, pause=True):
        for index, item in enumerate(self):
            if index and pause and not config['test_enable']:
                time.sleep(PAUSE)
            try:
                with self.env.cr.savepoint():
                    item._run_item()
            except (UserError, AccessError) as e:
                item.write({'state': 'failed', 'error': str(e)[:250]})
            except Exception:  # noqa: BLE001 - one bad item must not block the queue; the traceback is logged
                _logger.exception("WAHA queue item %s failed unexpectedly", item.id)
                item.write({'state': 'failed', 'error': "Unexpected error, see the server log."})
            if not config['test_enable']:
                self.env.cr.commit()

    @api.model
    def _cron_process(self):
        self.search([('state', '=', 'pending'), ('scheduled_for', '<=', fields.Datetime.now())],
                    order='id', limit=BATCH)._run_batch()

import re
import uuid

from odoo import api, fields, models
from odoo.exceptions import UserError


class WahaGroupSend(models.TransientModel):
    _name = 'elblasy.waha.group.send'
    _description = "Send a document to the customer and/or WhatsApp groups"

    res_model = fields.Char(readonly=True)
    res_id = fields.Integer(readonly=True)
    partner_id = fields.Many2one('res.partner', string="Customer")
    session_id = fields.Many2one('elblasy.waha.session', string="Session", domain="[('status', '=', 'WORKING')]")
    send_to_customer = fields.Boolean("Send to this number")
    phone = fields.Char("Number", help="International format, e.g. +20 100 123 4567")
    allowed_group_ids = fields.Many2many('elblasy.waha.group', compute='_compute_allowed_group_ids')
    group_ids = fields.Many2many('elblasy.waha.group', string="Groups", domain="[('id', 'in', allowed_group_ids)]")
    report_id = fields.Many2one(
        'ir.actions.report', string="Attach report", domain="[('model', '=', res_model), ('report_type', '=', 'qweb-pdf')]")
    attachment_ids = fields.Many2many('ir.attachment', string="Attachments")
    body = fields.Text("Message")
    token = fields.Char(default=lambda s: uuid.uuid4().hex, readonly=True)

    @api.depends('partner_id')
    def _compute_allowed_group_ids(self):
        Group = self.env['elblasy.waha.group']
        for wiz in self:
            linked = wiz.partner_id.waha_group_ids.filtered(lambda g: g.allow_send and g.active)
            wiz.allowed_group_ids = linked or Group.search([('allow_send', '=', True)])

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ctx = self.env.context
        record = self.env[ctx['active_model']].browse(ctx['active_id']).exists() \
            if ctx.get('active_model') and ctx.get('active_id') else None
        if record is not None and record:
            res.update(res_model=ctx['active_model'], res_id=ctx['active_id'], body=record.display_name)
            partner, phone = self.env['elblasy.waha.group.post']._record_targets(record)
            if partner:
                res['partner_id'] = partner.id
                linked = partner.waha_group_ids.filtered(lambda g: g.allow_send and g.active)
                res['group_ids'] = [(6, 0, linked.ids)]
            res['phone'] = phone
            res['send_to_customer'] = len(re.sub(r'\D', '', phone or '')) >= 7
        session = self.env['elblasy.waha.session'].search([('status', '=', 'WORKING')], limit=1)
        res.setdefault('session_id', session.id)
        return res

    def _build_attachments(self):
        attachments = self.attachment_ids
        if self.report_id and self.res_model and self.res_id:
            pdf, _fmt = self.env['ir.actions.report']._render_qweb_pdf(self.report_id, [self.res_id])
            record = self.env[self.res_model].browse(self.res_id)
            attachments |= self.env['ir.attachment'].create({
                'name': f"{record.display_name or self.report_id.name}.pdf", 'raw': pdf,
                'mimetype': 'application/pdf', 'res_model': 'elblasy.waha.group.send', 'res_id': 0})
        return attachments

    def action_send(self):
        self.ensure_one()
        if not self.group_ids and not self.send_to_customer:
            raise UserError(self.env._("Choose the customer and/or at least one group."))
        if self.send_to_customer and not (self.phone and self.session_id):
            raise UserError(self.env._("Enter the number and pick the WhatsApp session to send privately."))
        attachments = self._build_attachments()
        Post = self.env['elblasy.waha.group.post']
        posts = self.env['elblasy.waha.group.post']
        if self.send_to_customer:
            posts |= Post.send_to_number(self.phone, self.session_id, self.body, attachments,
                                         self.res_model, self.res_id, f"{self.token}-c", partner=self.partner_id)
        for group in self.group_ids:
            posts |= Post.send_to_group(group, self.body, attachments, self.res_model, self.res_id,
                                        f"{self.token}-{group.id}")
        failed = posts.filtered(lambda p: p.state == 'failed')
        unknown = posts.filtered(lambda p: p.state == 'unknown')
        if failed:
            raise UserError(self.env._("Not sent to: %s", ", ".join(failed.mapped('target_name'))))
        ok = len(posts) - len(unknown)
        message = self.env._("Sent to %s target(s).", ok)
        if unknown:
            message += " " + self.env._("Not confirmed (check before resending): %s", ", ".join(unknown.mapped('target_name')))
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': self.env._("WhatsApp"), 'type': 'warning' if unknown else 'success', 'message': message,
            'next': {'type': 'ir.actions.act_window_close'}}}

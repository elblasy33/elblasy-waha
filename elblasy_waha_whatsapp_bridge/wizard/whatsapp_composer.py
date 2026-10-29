from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

MAX_FREE_TEXT_RECIPIENTS = 20  # free text to many people is bulk messaging: keep it deliberate


class WhatsappComposer(models.TransientModel):
    _inherit = 'whatsapp.composer'

    waha_free_text = fields.Boolean(compute='_compute_waha_free_text')
    waha_account_id = fields.Many2one(
        'whatsapp.account', string="Send with", domain="[('waha_session_id', '!=', False)]",
        default=lambda self: self.env['whatsapp.account'].search([('waha_session_id', '!=', False)], limit=1))
    waha_message = fields.Text(
        "Message", help="Plain text. Users who can edit mail templates may use {{ object.name }} placeholders.")

    def _raise_no_template_error(self, res_model):
        # With a WAHA account the user can write a free message instead of choosing a template
        if self.env['whatsapp.account'].search_count([('waha_session_id', '!=', False)]):
            return
        return super()._raise_no_template_error(res_model)

    @api.depends('wa_template_id', 'waha_account_id')
    def _compute_waha_free_text(self):
        for composer in self:
            composer.waha_free_text = not composer.wa_template_id and bool(composer.waha_account_id)

    @api.depends('wa_template_id')
    @api.depends_context('default_phone')
    def _compute_number(self):
        super()._compute_number()
        for composer in self.filtered(lambda c: not c.wa_template_id and c.waha_account_id):
            records = composer._get_active_records()
            numbers = [info['number'] for info in records._phone_get_recipients_info().values() if info['number']] \
                if hasattr(records, '_phone_get_recipients_info') else []
            if numbers and not self.env.context.get('default_phone'):
                composer.phone = ', '.join(numbers) if composer.batch_mode else numbers[0]

    def action_send_whatsapp_template(self):
        self.ensure_one()
        if self.wa_template_id or not self.waha_free_text:
            return super().action_send_whatsapp_template()
        return self._waha_send_free_text()

    def _waha_send_free_text(self):
        """Free message through a WAHA account (no template, no 24h window), tracked in Discuss and on the document."""
        self.ensure_one()
        text = (self.waha_message or '').strip()
        if not text:
            raise ValidationError(self.env._("Write the message to send."))
        records = self._get_active_records()
        if len(records) > MAX_FREE_TEXT_RECIPIENTS:
            raise UserError(self.env._(
                "Free text can go to at most %s recipients at once; use a template or an automation rule for more.",
                MAX_FREE_TEXT_RECIPIENTS))
        render = self.env.user.has_group('mail.group_mail_template_editor')
        info = records._phone_get_recipients_info() if hasattr(records, '_phone_get_recipients_info') else {}
        sent = skipped = 0
        for record in records:
            number = (not self.batch_mode and self.phone) or info.get(record.id, {}).get('number')
            partner = info.get(record.id, {}).get('partner') or (record if record._name == 'res.partner' else False)
            if not number:
                skipped += 1
                continue
            body = self.env['mail.render.mixin']._render_template(
                text, record._name, record.ids, engine='inline_template')[record.id] if render else text
            self.waha_account_id._waha_start_chat(number=number, partner=partner or None, first_message=body)
            if hasattr(record, 'message_post'):
                record.message_post(body=Markup("%s: %s") % (self.env._("WhatsApp message sent"), body),
                                    subtype_xmlid='mail.mt_note')
            sent += 1
        if not sent:
            raise UserError(self.env._("No valid phone number to send to."))
        message = self.env._("WhatsApp message sent to %s recipient(s).", sent)
        if skipped:
            message += " " + self.env._("%s skipped (no phone number).", skipped)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': self.env._("WhatsApp"), 'message': message, 'type': 'success',
            'next': {'type': 'ir.actions.act_window_close'}}}

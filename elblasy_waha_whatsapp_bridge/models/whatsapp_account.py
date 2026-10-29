import re

from odoo import fields, models
from odoo.exceptions import UserError


class WhatsappAccount(models.Model):
    _inherit = 'whatsapp.account'

    waha_session_id = fields.Many2one('elblasy.waha.session', string="WAHA Session", copy=False,
                                      index='btree_not_null', ondelete='set null', readonly=True)

    _waha_session_uniq = models.Constraint(
        'UNIQUE (waha_session_id)', "This WAHA session is already linked to a WhatsApp account.")

    def _waha_start_chat(self, number=None, partner=None, first_message=None):
        """Find or create the Discuss channel to talk to any phone number, optionally posting a first message.

        With WAHA there is no template / 24h restriction, so the conversation can start with a free text.
        """
        self.ensure_one()
        digits = re.sub(r'\D', '', number or (partner and partner.phone) or '')
        digits = digits[2:] if digits.startswith('00') else digits
        if len(digits) < 7:
            raise UserError(self.env._("Enter a valid phone number with the country code."))
        Channel = self.env['discuss.channel']
        channel = Channel.sudo()._get_whatsapp_channel_from_identifiers(
            self, {'number': digits}, sender_name=partner.name if partner else False, create_if_not_found=True)
        if not channel:
            raise UserError(self.env._("Could not open a conversation with this number."))
        user_partner = self.env.user.partner_id
        if user_partner not in channel.channel_member_ids.partner_id:
            channel.sudo().channel_member_ids = [(0, 0, {'partner_id': user_partner.id})]
        if partner and channel.whatsapp_partner_id != partner and not channel.whatsapp_partner_id.sudo().user_ids:
            # keep the chat attached to the contact the user chose when the number matched nobody yet
            channel.sudo().whatsapp_partner_id = partner
        if first_message and first_message.strip():
            channel.with_user(self.env.user).message_post(
                body=first_message.strip(), message_type='whatsapp_message', subtype_xmlid='mail.mt_comment',
                author_id=user_partner.id)
        return channel

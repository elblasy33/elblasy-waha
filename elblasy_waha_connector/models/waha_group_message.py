from odoo import fields, models


class WahaGroupMessage(models.Model):
    """Messages written in a group (stored only for groups with ``log_messages``)."""
    _name = 'elblasy.waha.group.message'
    _description = "WhatsApp Group Message"
    _order = 'date desc, id desc'

    group_id = fields.Many2one('elblasy.waha.group', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='group_id.company_id', store=True, index=True)
    remote_message_id = fields.Char(required=True, index=True)
    sender_id = fields.Char("Sender")
    sender_name = fields.Char()
    from_me = fields.Boolean()
    message_type = fields.Selection([('text', "Text"), ('media', "Media"), ('other', "Other")], default='text')
    body = fields.Text()
    date = fields.Datetime(required=True, index=True)

    _remote_uniq = models.Constraint('UNIQUE (group_id, remote_message_id)', "Message already stored.")

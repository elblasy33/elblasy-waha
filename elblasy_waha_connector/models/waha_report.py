"""Read-only analysis models (SQL views) behind WhatsApp ▸ Reporting.

Hours and weekdays are computed in the time zone of the company (its address), else of the administrator,
else UTC, so "peak hours" match the clock of the people answering.
"""
from odoo import fields, models
from odoo.tools import SQL

WEEKDAYS = [('1', "Monday"), ('2', "Tuesday"), ('3', "Wednesday"), ('4', "Thursday"), ('5', "Friday"),
            ('6', "Saturday"), ('7', "Sunday")]
CHAT_TYPES = [('user', "Contact"), ('lid', "Hidden ID"), ('group', "Group"), ('other', "Other")]

LOCAL_TZ = """COALESCE(
    NULLIF((SELECT p.tz FROM res_company co JOIN res_partner p ON p.id = co.partner_id WHERE co.id = m.company_id), ''),
    NULLIF((SELECT p.tz FROM res_users u JOIN res_partner p ON p.id = u.partner_id WHERE u.id = 2), ''),
    'UTC')"""


def _flush(env, model, names):
    return [env[model]._fields[name] for name in names]


class WahaReportMessage(models.Model):
    """One row per message, with the chat, the contact and the local hour / weekday."""
    _name = 'elblasy.waha.report.message'
    _description = "WhatsApp Message Analysis"
    _auto = False
    _rec_name = 'date'
    _order = 'date desc'

    message_id = fields.Many2one('elblasy.waha.message', readonly=True)
    nbr = fields.Integer("Messages", readonly=True)
    date = fields.Datetime(readonly=True)
    hour = fields.Integer("Hour", readonly=True, aggregator=None)
    weekday = fields.Selection(WEEKDAYS, readonly=True)
    session_id = fields.Many2one('elblasy.waha.session', "WhatsApp Number", readonly=True)
    company_id = fields.Many2one('res.company', readonly=True)
    conversation_id = fields.Many2one('elblasy.waha.conversation', "Chat", readonly=True)
    partner_id = fields.Many2one('res.partner', "Contact", readonly=True)
    phone = fields.Char(readonly=True)
    chat_type = fields.Selection(CHAT_TYPES, readonly=True)
    direction = fields.Selection([('in', "Incoming"), ('out', "Outgoing")], readonly=True)
    message_type = fields.Selection(
        [('text', "Text"), ('image', "Image"), ('video', "Video"), ('audio', "Audio"), ('document', "Document"),
         ('sticker', "Sticker"), ('location', "Location"), ('contact', "Contact card"), ('poll', "Poll"),
         ('other', "Other")], "Type", readonly=True)
    delivery_status = fields.Selection(
        [('pending', "Sending"), ('sent', "Sent"), ('delivered', "Delivered"), ('read', "Read"), ('failed', "Failed"),
         ('unknown', "Not confirmed")], readonly=True)
    user_id = fields.Many2one('res.users', "Agent", readonly=True)
    author_name = fields.Char("Group member", readonly=True)
    is_auto = fields.Boolean("Automatic", readonly=True)
    has_media = fields.Boolean("With a file", readonly=True)
    body = fields.Text(readonly=True)
    response_minutes = fields.Float(
        "Response time (min)", readonly=True, aggregator='avg', digits=(16, 1),
        help="Human replies only: minutes between the customer's first unanswered message and the reply.")

    @property
    def _table_sql(self):
        return SQL(f"""(
            SELECT m.id, m.id AS message_id, 1 AS nbr, m.date,
                   EXTRACT(hour FROM m.date AT TIME ZONE 'UTC' AT TIME ZONE {LOCAL_TZ})::int AS hour,
                   EXTRACT(isodow FROM m.date AT TIME ZONE 'UTC' AT TIME ZONE {LOCAL_TZ})::int::text AS weekday,
                   m.session_id, m.company_id, m.conversation_id, c.partner_id, c.remote_phone AS phone,
                   c.chat_type, m.direction, m.message_type, m.delivery_status, m.user_id, m.author_name,
                   COALESCE(m.is_auto, FALSE) AS is_auto,
                   (m.message_type IN ('image', 'video', 'audio', 'document', 'sticker')) AS has_media,
                   m.body,
                   CASE WHEN m.response_seconds > 0 THEN m.response_seconds / 60.0 END AS response_minutes
              FROM elblasy_waha_message m
              JOIN elblasy_waha_conversation c ON c.id = m.conversation_id
        )""", to_flush=_flush(self.env, 'elblasy.waha.message', [
            'date', 'conversation_id', 'direction', 'message_type', 'delivery_status', 'user_id', 'is_auto',
            'response_seconds', 'body', 'author_name']) + _flush(self.env, 'elblasy.waha.conversation', [
            'partner_id', 'remote_phone', 'chat_type']))

    def action_open_chat(self):
        self.ensure_one()
        return self.conversation_id.action_open_inbox()


class WahaReportChat(models.Model):
    """One row per chat: volumes, first and last contact, response times, who is waiting for an answer."""
    _name = 'elblasy.waha.report.chat'
    _description = "WhatsApp Chat Analysis"
    _auto = False
    _rec_name = 'conversation_id'
    _order = 'last_date desc'

    conversation_id = fields.Many2one('elblasy.waha.conversation', "Chat", readonly=True)
    nbr = fields.Integer("Chats", readonly=True)
    session_id = fields.Many2one('elblasy.waha.session', "WhatsApp Number", readonly=True)
    company_id = fields.Many2one('res.company', readonly=True)
    partner_id = fields.Many2one('res.partner', "Contact", readonly=True)
    phone = fields.Char(readonly=True)
    chat_type = fields.Selection(CHAT_TYPES, readonly=True)
    first_date = fields.Datetime("First message", readonly=True)
    last_date = fields.Datetime("Last message", readonly=True)
    last_incoming_date = fields.Datetime("Last message from them", readonly=True)
    incoming_count = fields.Integer("Received", readonly=True)
    outgoing_count = fields.Integer("Sent", readonly=True)
    auto_count = fields.Integer("Automatic", readonly=True)
    media_count = fields.Integer("Files", readonly=True)
    failed_count = fields.Integer("Failed", readonly=True)
    avg_response_minutes = fields.Float("Avg response (min)", readonly=True, aggregator='avg', digits=(16, 1))
    max_response_minutes = fields.Float("Slowest response (min)", readonly=True, aggregator='max', digits=(16, 1))
    awaiting_since = fields.Datetime("Waiting since", readonly=True)
    waiting = fields.Boolean("Waiting for a reply", readonly=True)
    days_silent = fields.Integer("Days since last message", readonly=True, aggregator='avg')

    @property
    def _table_sql(self):
        return SQL("""(
            SELECT c.id, c.id AS conversation_id, 1 AS nbr, c.session_id, c.company_id, c.partner_id,
                   c.remote_phone AS phone, c.chat_type,
                   MIN(m.date) AS first_date, MAX(m.date) AS last_date,
                   MAX(m.date) FILTER (WHERE m.direction = 'in') AS last_incoming_date,
                   COUNT(m.id) FILTER (WHERE m.direction = 'in') AS incoming_count,
                   COUNT(m.id) FILTER (WHERE m.direction = 'out') AS outgoing_count,
                   COUNT(m.id) FILTER (WHERE m.is_auto) AS auto_count,
                   COUNT(m.id) FILTER (WHERE m.message_type IN ('image', 'video', 'audio', 'document', 'sticker'))
                       AS media_count,
                   COUNT(m.id) FILTER (WHERE m.delivery_status = 'failed') AS failed_count,
                   AVG(m.response_seconds) FILTER (WHERE m.response_seconds > 0) / 60.0 AS avg_response_minutes,
                   MAX(m.response_seconds) FILTER (WHERE m.response_seconds > 0) / 60.0 AS max_response_minutes,
                   c.awaiting_since, (c.awaiting_since IS NOT NULL) AS waiting,
                   (CURRENT_DATE - MAX(m.date)::date) AS days_silent
              FROM elblasy_waha_conversation c
              LEFT JOIN elblasy_waha_message m ON m.conversation_id = c.id
             WHERE c.active
          GROUP BY c.id
        )""", to_flush=_flush(self.env, 'elblasy.waha.message', [
            'date', 'conversation_id', 'direction', 'message_type', 'delivery_status', 'is_auto',
            'response_seconds']) + _flush(self.env, 'elblasy.waha.conversation', [
            'partner_id', 'remote_phone', 'chat_type', 'awaiting_since', 'active']))

    def action_open_chat(self):
        self.ensure_one()
        return self.conversation_id.action_open_inbox()

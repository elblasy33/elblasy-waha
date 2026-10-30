"""Read-only analysis models (SQL views) behind WhatsApp ▸ Reporting.

Hours and weekdays are computed in the time zone of the company (its address), else of the administrator,
else UTC, so "peak hours" match the clock of the people answering.
"""
import logging

from odoo import fields, models
from odoo.tools import SQL

_logger_report = logging.getLogger(__name__)

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


DOC_STATES = [('pending', "Sending"), ('sent', "Sent"), ('failed', "Failed")]


class WahaReportDocument(models.Model):
    """Every document sent on WhatsApp, whatever its model and however it was sent."""
    _name = 'elblasy.waha.report.document'
    _description = "Documents Sent on WhatsApp"
    _auto = False
    _rec_name = 'res_model'
    _order = 'date desc'

    nbr = fields.Integer("Sends", readonly=True)
    date = fields.Datetime(readonly=True)
    source = fields.Selection([('waha', "Send to WhatsApp / automation"), ('template', "Odoo WhatsApp template")],
                              readonly=True)
    channel = fields.Selection([('customer', "Customer chat"), ('group', "Group")], readonly=True)
    model_id = fields.Many2one('ir.model', "Document type", readonly=True)
    res_model = fields.Char(readonly=True)
    res_id = fields.Integer(readonly=True)
    template = fields.Char(readonly=True)
    partner_id = fields.Many2one('res.partner', "Customer", readonly=True)
    phone = fields.Char(readonly=True)
    session_id = fields.Many2one('elblasy.waha.session', "WhatsApp Number", readonly=True)
    user_id = fields.Many2one('res.users', "Sent by", readonly=True)
    company_id = fields.Many2one('res.company', readonly=True)
    state = fields.Selection(DOC_STATES, readonly=True)
    record_name = fields.Char("Document", compute='_compute_record_name')

    def _compute_record_name(self):
        for row in self:
            record = row._record()
            row.record_name = record.display_name if record else (row.res_model or '')

    def _record(self):
        self.ensure_one()
        if not self.res_model or self.res_model not in self.env or not self.res_id:
            return None
        record = self.env[self.res_model].browse(self.res_id).exists()
        return record if record and record.has_access('read') else None

    def action_open_record(self):
        self.ensure_one()
        if not self._record():
            return False
        return {'type': 'ir.actions.act_window', 'res_model': self.res_model, 'res_id': self.res_id,
                'views': [(False, 'form')]}

    def _document_sources(self):
        """SELECTs with the columns of this view, one per way of sending (extended by the bridge)."""
        return ["""
            SELECT p.id * 10 + 1 AS id, 1 AS nbr, p.create_date AS date, 'waha' AS source,
                   CASE WHEN p.group_id IS NOT NULL THEN 'group' ELSE 'customer' END AS channel,
                   p.res_model, p.res_id, NULL::varchar AS template, p.partner_id,
                   NULL::varchar AS phone, p.session_id, p.user_id, p.company_id,
                   CASE WHEN p.state IN ('sent', 'unknown') THEN 'sent' WHEN p.state = 'failed' THEN 'failed'
                        ELSE 'pending' END AS state
              FROM elblasy_waha_group_post p
        """]

    @property
    def _table_sql(self):
        union = " UNION ALL ".join(self._document_sources())
        return SQL(f"""(
            SELECT d.*, im.id AS model_id FROM ({union}) d
              LEFT JOIN ir_model im ON im.model = d.res_model
        )""", to_flush=_flush(self.env, 'elblasy.waha.group.post', [
            'state', 'res_model', 'res_id', 'partner_id', 'group_id']))


class WahaReportBusiness(models.Model):
    """Business documents of any tracked model, with the WhatsApp activity of their customer before them."""
    _name = 'elblasy.waha.report.business'
    _description = "WhatsApp Business Impact"
    _auto = False
    _rec_name = 'res_model'
    _order = 'date desc'

    nbr = fields.Integer("Documents", readonly=True)
    tracked_model_id = fields.Many2one('elblasy.waha.tracked.model', "Document type", readonly=True)
    res_model = fields.Char(readonly=True)
    res_id = fields.Integer(readonly=True)
    date = fields.Datetime(readonly=True)
    partner_id = fields.Many2one('res.partner', "Customer", readonly=True)
    company_id = fields.Many2one('res.company', readonly=True)
    amount = fields.Float(readonly=True, digits=(16, 2))
    from_whatsapp = fields.Boolean(
        "After a WhatsApp chat", readonly=True,
        help="The customer exchanged WhatsApp messages in the window before this document.")
    whatsapp_customer = fields.Boolean("WhatsApp customer", readonly=True,
                                       help="The customer has a WhatsApp chat (at any time).")
    sent_on_whatsapp = fields.Boolean("Sent on WhatsApp", readonly=True)
    whatsapp_amount = fields.Float("Amount after WhatsApp", readonly=True, digits=(16, 2))
    days_after_chat = fields.Float("Days after the chat", readonly=True, aggregator='avg', digits=(16, 1))
    record_name = fields.Char("Document", compute='_compute_record_name')

    _compute_record_name = WahaReportDocument._compute_record_name
    _record = WahaReportDocument._record
    action_open_record = WahaReportDocument.action_open_record

    def _sources(self):
        selects = []
        for tracked in self.env['elblasy.waha.tracked.model'].sudo().search([]):
            if tracked.model not in self.env:
                continue
            Model = self.env[tracked.model].sudo()
            try:
                query = Model._search(tracked._parsed_domain())
            except Exception as e:  # noqa: BLE001 - a broken configuration must not break every report
                _logger_report.warning("Tracked document %s skipped: %s", tracked.id, e)
                continue
            table = query.table
            company = table['company_id'] if 'company_id' in Model._fields and Model._fields['company_id'].store \
                else SQL("NULL::int")
            amount = SQL("COALESCE(%s, 0)::float", table[tracked.amount_field_id.name]) if tracked.amount_field_id \
                else SQL("0::float")
            selects.append(SQL("(%s)", query.select(
                SQL("%s * 1000 + %s AS id", table.id, tracked.id),
                SQL("%s AS tracked_model_id", tracked.id),
                SQL("%s::varchar AS res_model", tracked.model),
                SQL("%s AS res_id", table.id),
                SQL("%s::timestamp AS date", table[tracked.date_field_id.name]),
                SQL("%s AS partner_id", table[tracked.partner_field_id.name]),
                SQL("%s AS company_id", company),
                SQL("%s AS amount", amount),
                SQL("%s AS window_days", tracked.window_days),
            )))
        return selects

    @property
    def _table_sql(self):
        sources = self._sources()
        if not sources:
            sources = [SQL("""SELECT NULL::int AS id, NULL::int AS tracked_model_id, NULL::varchar AS res_model,
                                     NULL::int AS res_id, NULL::timestamp AS date, NULL::int AS partner_id,
                                     NULL::int AS company_id, 0::float AS amount, 0 AS window_days WHERE FALSE""")]
        documents = self.env['elblasy.waha.report.document']._table_sql
        return SQL("""(
            SELECT d.id, 1 AS nbr, d.tracked_model_id, d.res_model, d.res_id, d.date, d.partner_id, d.company_id,
                   d.amount,
                   (chat.last_date IS NOT NULL) AS from_whatsapp,
                   EXISTS (SELECT 1 FROM elblasy_waha_conversation c2
                             JOIN res_partner cp2 ON cp2.id = c2.partner_id
                            WHERE cp2.commercial_partner_id = p.commercial_partner_id) AS whatsapp_customer,
                   EXISTS (SELECT 1 FROM %s s WHERE s.res_model = d.res_model AND s.res_id = d.res_id
                                                AND s.state = 'sent') AS sent_on_whatsapp,
                   CASE WHEN chat.last_date IS NOT NULL THEN d.amount ELSE 0 END AS whatsapp_amount,
                   EXTRACT(epoch FROM d.date - chat.last_date) / 86400.0 AS days_after_chat
              FROM (%s) d
              LEFT JOIN res_partner p ON p.id = d.partner_id
              LEFT JOIN LATERAL (
                    SELECT MAX(m.date) AS last_date
                      FROM elblasy_waha_message m
                      JOIN elblasy_waha_conversation c ON c.id = m.conversation_id
                      JOIN res_partner cp ON cp.id = c.partner_id
                     WHERE cp.commercial_partner_id = p.commercial_partner_id
                       AND m.date <= d.date
                       AND m.date >= d.date - make_interval(days => d.window_days)
              ) chat ON TRUE
             WHERE d.date IS NOT NULL
        )""", documents, SQL(" UNION ALL ").join(sources), to_flush=_flush(
            self.env, 'elblasy.waha.message', ['date', 'conversation_id']) + _flush(
            self.env, 'elblasy.waha.conversation', ['partner_id']) + _flush(
            self.env, 'res.partner', ['commercial_partner_id']))

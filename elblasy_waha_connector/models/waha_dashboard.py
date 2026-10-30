from datetime import datetime, time, timedelta

import pytz

from odoo import api, fields, models
from odoo.exceptions import AccessError

MANAGER = 'elblasy_waha_connector.group_waha_manager'


class WahaDashboard(models.AbstractModel):
    """Numbers of the WhatsApp dashboard (today in the user's time zone, compared with yesterday)."""
    _name = 'elblasy.waha.dashboard'
    _description = "WhatsApp Dashboard"

    @api.model
    def get_data(self, session_id=None):
        if not self.env.user.has_group(MANAGER):
            raise AccessError(self.env._("The WhatsApp dashboard is for WhatsApp managers."))
        tz = pytz.timezone(self.env.context.get('tz') or self.env.user.tz or 'UTC')
        today = datetime.now(tz).date()

        def utc(day, delta=0):
            local = tz.localize(datetime.combine(day + timedelta(days=delta), time.min))
            return local.astimezone(pytz.utc).replace(tzinfo=None)

        start, end, y_start = utc(today), utc(today, 1), utc(today, -1)
        company_ids = tuple(self.env.companies.ids)
        session_ids = tuple(self.env['elblasy.waha.session'].search(
            [('id', '=', session_id)] if session_id else []).ids) or (0,)
        cr = self.env.cr

        def messages(date_from, date_to):
            cr.execute("""
                SELECT COUNT(*) FILTER (WHERE m.direction = 'in'),
                       COUNT(*) FILTER (WHERE m.direction = 'out' AND NOT COALESCE(m.is_auto, FALSE)),
                       COUNT(*) FILTER (WHERE m.is_auto),
                       COUNT(*) FILTER (WHERE m.delivery_status = 'failed'),
                       AVG(m.response_seconds) FILTER (WHERE m.response_seconds > 0) / 60.0,
                       COUNT(*) FILTER (WHERE m.response_seconds > 0),
                       COUNT(*) FILTER (WHERE m.response_seconds > 0 AND m.response_seconds <= 900)
                  FROM elblasy_waha_message m
                 WHERE m.date >= %s AND m.date < %s AND m.company_id IN %s AND m.session_id IN %s
            """, [date_from, date_to, company_ids, session_ids])
            received, sent, auto, failed, avg_resp, replies, fast = cr.fetchone()
            return {'received': received, 'sent': sent, 'auto': auto, 'failed': failed,
                    'avg_response': round(avg_resp, 1) if avg_resp is not None else None,
                    'fast_pct': round(100.0 * fast / replies) if replies else None}

        def new_chats(date_from, date_to):
            cr.execute("""
                SELECT COUNT(*) FROM (
                    SELECT m.conversation_id FROM elblasy_waha_message m
                     WHERE m.company_id IN %s AND m.session_id IN %s
                     GROUP BY m.conversation_id HAVING MIN(m.date) >= %s AND MIN(m.date) < %s) first
            """, [company_ids, session_ids, date_from, date_to])
            return cr.fetchone()[0]

        Conv = self.env['elblasy.waha.conversation']
        waiting_domain = [('awaiting_since', '!=', False), ('chat_type', '!=', 'group'),
                          ('session_id', 'in', list(session_ids))]
        waiting = Conv.search(waiting_domain, order='awaiting_since asc', limit=8)
        now = fields.Datetime.now()

        # last 14 days, received / sent per local day
        cr.execute("""
            SELECT (m.date AT TIME ZONE 'UTC' AT TIME ZONE %s)::date AS day,
                   COUNT(*) FILTER (WHERE m.direction = 'in'), COUNT(*) FILTER (WHERE m.direction = 'out')
              FROM elblasy_waha_message m
             WHERE m.date >= %s AND m.company_id IN %s AND m.session_id IN %s
             GROUP BY 1 ORDER BY 1
        """, [tz.zone, utc(today, -13), company_ids, session_ids])
        per_day = {row[0]: row[1:] for row in cr.fetchall()}
        days = []
        for offset in range(-13, 1):
            day = today + timedelta(days=offset)
            received, sent = per_day.get(day, (0, 0))
            days.append({'date': fields.Date.to_string(day), 'received': received, 'sent': sent})

        # received today per local hour
        cr.execute("""
            SELECT EXTRACT(hour FROM m.date AT TIME ZONE 'UTC' AT TIME ZONE %s)::int, COUNT(*)
              FROM elblasy_waha_message m
             WHERE m.direction = 'in' AND m.date >= %s AND m.date < %s AND m.company_id IN %s AND m.session_id IN %s
             GROUP BY 1
        """, [tz.zone, start, end, company_ids, session_ids])
        per_hour = dict(cr.fetchall())

        # agents today
        cr.execute("""
            SELECT m.user_id, COUNT(*), AVG(m.response_seconds) FILTER (WHERE m.response_seconds > 0) / 60.0
              FROM elblasy_waha_message m
             WHERE m.direction = 'out' AND NOT COALESCE(m.is_auto, FALSE) AND m.user_id IS NOT NULL
               AND m.date >= %s AND m.date < %s AND m.company_id IN %s AND m.session_id IN %s
             GROUP BY 1 ORDER BY 2 DESC LIMIT 6
        """, [start, end, company_ids, session_ids])
        agents = [{'name': self.env['res.users'].browse(uid).name, 'replies': count,
                   'avg_response': round(avg, 1) if avg is not None else None} for uid, count, avg in cr.fetchall()]

        # documents sent today, by type (any model)
        Documents = self.env['elblasy.waha.report.document']
        doc_groups = Documents._read_group([('date', '>=', start), ('date', '<', end), ('state', '=', 'sent')],
                                           ['model_id'], ['nbr:sum'])
        documents = [{'model': model.name if model else self.env._("Message"), 'count': count}
                     for model, count in doc_groups]

        # business impact, last 30 days, per tracked document type
        Business = self.env['elblasy.waha.report.business']
        month = utc(today, -29)
        business = []
        for tracked, count, amount, wa_amount in Business._read_group(
                [('date', '>=', month)], ['tracked_model_id'], ['nbr:sum', 'amount:sum', 'whatsapp_amount:sum']):
            from_count = Business.search_count([('date', '>=', month), ('tracked_model_id', '=', tracked.id),
                                                ('from_whatsapp', '=', True)])
            business.append({'id': tracked.id, 'name': tracked.name, 'count': count, 'from_whatsapp': from_count,
                             'pct': round(100.0 * from_count / count) if count else 0,
                             'amount': amount, 'whatsapp_amount': wa_amount,
                             'has_amount': bool(tracked.amount_field_id)})

        return {
            'today': {**messages(start, end), 'new_chats': new_chats(start, end),
                      'documents': sum(d['count'] for d in documents),
                      'optouts': self.env['elblasy.waha.optout'].search_count([('create_date', '>=', start)])},
            'yesterday': {**messages(y_start, start), 'new_chats': new_chats(y_start, start)},
            'waiting_count': Conv.search_count(waiting_domain),
            'waiting': [{'id': c.id, 'name': c.name, 'minutes': int((now - c.awaiting_since).total_seconds() // 60),
                         'preview': c.last_message_preview or ''} for c in waiting],
            'days': days,
            'hours': [per_hour.get(hour, 0) for hour in range(24)],
            'agents': agents,
            'documents': documents,
            'business': business,
            'sessions': [{'id': s.id, 'name': s.name, 'status': s.status}
                         for s in self.env['elblasy.waha.session'].search([])],
            'currency': self.env.company.currency_id.symbol or '',
            'range': {'start': fields.Datetime.to_string(start), 'end': fields.Datetime.to_string(end),
                      'month': fields.Datetime.to_string(month)},
        }

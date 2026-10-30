import hashlib
import json
import logging
import mimetypes
from datetime import timedelta

import psycopg2

from odoo import api, fields, models

from .waha_client import WahaError
from .waha_message import ACK_MAP, ACK_NAME_MAP, ts_to_datetime

_logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
BATCH_SIZE = 500
RETRY_DELAY = timedelta(minutes=2)  # lets an ack that overtook its message wait for it
IGNORED_SUFFIXES = ('g.us', 'broadcast', 'newsletter')


class WahaWebhookEvent(models.Model):
    """Inbox of received webhook events: gives idempotency, retries and an audit trail."""
    _name = 'elblasy.waha.webhook.event'
    _description = "WAHA Webhook Event"
    _order = 'id desc'

    session_id = fields.Many2one('elblasy.waha.session', required=True, ondelete='cascade', index=True)
    dedupe_key = fields.Char(required=True, index=True)
    event_type = fields.Char(index=True)
    payload = fields.Text(groups='elblasy_waha_connector.group_waha_admin',
                          help="Raw event; may contain message content.")
    state = fields.Selection([('pending', "Pending"), ('done', "Done"), ('failed', "Failed"), ('ignored', "Ignored")],
                             default='pending', index=True)
    attempts = fields.Integer(default=0)
    error = fields.Char()

    _dedupe_uniq = models.Constraint('UNIQUE (session_id, dedupe_key)', "Duplicate webhook event.")

    @api.model
    def _store_event(self, session, raw_body, data):
        """Store the event once. Returns the new record or an empty recordset for duplicates."""
        key = data.get('id') or hashlib.sha256(raw_body).hexdigest()
        if self.search_count([('session_id', '=', session.id), ('dedupe_key', '=', key)]):
            return self.browse()
        try:
            with self.env.cr.savepoint():
                return self.create({
                    'session_id': session.id, 'dedupe_key': key, 'event_type': str(data.get('event'))[:64],
                    'payload': raw_body.decode('utf-8', 'replace'),
                })
        except psycopg2.errors.UniqueViolation:
            return self.browse()  # the same event arrived twice at the same time; any other error must surface

    def _schedule_processing(self):
        """Wake the cron worker up; process inline only when that cron was disabled."""
        cron = self.env.ref('elblasy_waha_connector.cron_waha_retry_events', raise_if_not_found=False)
        if cron and cron.sudo().active:
            cron.sudo()._trigger()
        else:
            self._process_event()

    def _process_event(self):
        for event in self:
            if event.state not in ('pending', 'failed'):
                continue
            try:
                with self.env.cr.savepoint():
                    data = json.loads(event.sudo().payload)
                    state = event._dispatch(data)
                event.write({'state': state, 'error': False, 'attempts': event.attempts + 1})
            except ValueError as e:
                # malformed payload: retrying cannot help, keep it visible for an administrator
                _logger.info("WAHA event %s is malformed: %s", event.id, e)
                event.write({'state': 'failed', 'attempts': MAX_ATTEMPTS, 'error': f"ValueError: {e}"[:250]})
            except LookupError as e:
                # an ack that overtook its message is retried by the cron; acks of messages Odoo never had
                # (typed on the phone before the sync, older than Odoo...) are dropped after the last attempt
                attempts = event.attempts + 1
                _logger.info("WAHA event %s: %s (attempt %s/%s)", event.id, e, attempts, MAX_ATTEMPTS)
                event.write({'state': 'ignored' if attempts >= MAX_ATTEMPTS else 'failed', 'attempts': attempts,
                             'error': f"LookupError: {e}"[:250]})
            except Exception as e:  # noqa: BLE001 - one bad event must not block the others; the traceback is logged
                _logger.warning("WAHA event %s failed unexpectedly: %s", event.id, type(e).__name__, exc_info=True)
                event.write({'state': 'failed', 'attempts': event.attempts + 1, 'error': type(e).__name__})

    def _dispatch(self, data):
        session = self.session_id.sudo()
        etype = data.get('event')
        payload = data.get('payload')
        if not isinstance(payload, dict):
            return 'ignored'
        if etype == 'session.status':
            remote = {'status': payload.get('status')}
            if data.get('me'):
                remote['me'] = data['me']
            if data.get('engine'):
                remote['engine'] = data['engine']
            if isinstance(payload.get('data'), dict):
                remote['limits'] = payload['data']
            session._apply_remote(remote)
            if payload.get('status') == 'SCAN_QR_CODE':
                try:
                    session._fetch_qr()
                except Exception:  # noqa: BLE001 - QR is best effort here
                    _logger.info("QR fetch after status event failed for session %s", session.id)
            return 'done'
        if etype == 'message.any':
            return self._handle_message(session, payload)
        if etype == 'message.ack':
            return self._handle_ack(session, payload)
        if etype in ('group.v2.update', 'group.v2.participants'):
            return self._handle_group_event(session, etype, payload)
        if etype.startswith('label.'):
            return self._handle_label_event(session, etype, payload)
        if etype == 'call.received':
            return self._handle_call(session, payload)
        return 'ignored'

    def _handle_group_event(self, session, etype, p):
        info = p.get('group') if isinstance(p.get('group'), dict) else p
        group_id = info.get('id')
        group_id = group_id.get('_serialized') if isinstance(group_id, dict) else group_id
        group = self.env['elblasy.waha.group'].sudo().with_context(active_test=False).search(
            [('session_id', '=', session.id), ('chat_id', '=', group_id)], limit=1) if group_id else None
        if not group:
            return 'ignored'
        if etype == 'group.v2.update':
            vals = {k: v for k, v in (('name', info.get('subject') or info.get('name')),
                                      ('description', info.get('description'))) if v}
            if vals:
                group.write(vals)
        else:
            group.members_synced_at = False  # member list changed: needs a new sync
        return 'done'

    def _handle_label_event(self, session, etype, p):
        Label = self.env['elblasy.waha.label'].sudo()
        if etype == 'label.upsert':
            return 'done' if Label._upsert(session, p) else 'ignored'
        if etype == 'label.deleted':
            Label.search([('session_id', '=', session.id), ('label_id', '=', str(p.get('id')))]).unlink()
            return 'done'
        label = Label.search([('session_id', '=', session.id), ('label_id', '=', str(p.get('labelId')))], limit=1)
        if not label and isinstance(p.get('label'), dict):
            label = Label._upsert(session, p['label'])
        conv = self.env['elblasy.waha.conversation'].sudo().with_context(active_test=False).search(
            [('session_id', '=', session.id), ('remote_chat_id', '=', p.get('chatId'))], limit=1)
        if not label or not conv:
            return 'ignored'  # label unknown yet or chat not in Odoo: a manual "Sync Labels" fixes both
        conv.label_ids = [(4 if etype == 'label.chat.added' else 3, label.id)]
        return 'done'

    CALL_REPLY_COOLDOWN_MINUTES = 60

    def _handle_call(self, session, p):
        if not session.reject_calls or not p.get('id') or not isinstance(p.get('from'), str):
            return 'ignored'
        caller = p['from']
        Call = self.env['elblasy.waha.call'].sudo()
        client = session.server_id._get_client()
        recent = fields.Datetime.subtract(fields.Datetime.now(), minutes=self.CALL_REPLY_COOLDOWN_MINUTES)
        already_told = Call.search_count([('session_id', '=', session.id), ('chat_id', '=', caller),
                                          ('replied', '=', True), ('date', '>=', recent)])
        call = Call.create({'session_id': session.id, 'chat_id': caller, 'remote_call_id': p['id'],
                            'is_video': bool(p.get('isVideo'))})
        client.post(f'/api/{session.session_name}/calls/reject', {'from': caller, 'id': p['id']})
        call.rejected = True
        message = (session.reject_calls_message or '').strip()
        if message and not already_told and caller.rpartition('@')[2] in ('c.us', 'lid'):
            client.send_text(session.session_name, caller, message)
            call.replied = True
        return 'done'

    def _handle_group_message(self, session, p, chat_id):
        group = self.env['elblasy.waha.group'].sudo().with_context(active_test=False).search(
            [('session_id', '=', session.id), ('chat_id', '=', chat_id)], limit=1)
        if not group or not group.log_messages:
            return 'ignored'
        Message = self.env['elblasy.waha.group.message'].sudo()
        if Message.search_count([('group_id', '=', group.id), ('remote_message_id', '=', p['id'])]):
            return 'done'
        data = p.get('_data') if isinstance(p.get('_data'), dict) else {}
        has_media = bool(p.get('hasMedia'))
        Message.create({
            'group_id': group.id, 'remote_message_id': p['id'], 'from_me': bool(p.get('fromMe')),
            'sender_id': p.get('participant') or p.get('from'),
            'sender_name': data.get('notifyName') or data.get('pushName'),
            'message_type': 'media' if has_media else ('text' if p.get('body') else 'other'),
            'body': p.get('body') or ('[media]' if has_media else False),
            'date': ts_to_datetime(p.get('timestamp')),
        })
        return 'done'

    def _handle_message(self, session, p):
        remote_id = p.get('id')
        from_me = bool(p.get('fromMe'))
        chat_id = p.get('to') if from_me else p.get('from')
        if not remote_id or not isinstance(chat_id, str):
            raise ValueError("invalid message payload")
        if chat_id.endswith('@g.us'):
            return self._handle_group_message(session, p, chat_id)  # stored only for opted-in groups
        if chat_id.rpartition('@')[2] in IGNORED_SUFFIXES or chat_id.startswith('status@'):
            return 'ignored'  # status / channels
        Message = self.env['elblasy.waha.message'].sudo()
        existing = Message.search([('session_id', '=', session.id), ('remote_message_id', '=', remote_id)], limit=1)
        if existing:
            if p.get('ack') is not None and from_me:
                existing._set_ack(ACK_MAP.get(p['ack']))
            return 'done'
        data = p.get('_data') if isinstance(p.get('_data'), dict) else {}
        conv = self.env['elblasy.waha.conversation'].sudo()._get_or_create(
            session, chat_id, None if from_me else data.get('notifyName') or data.get('pushName'))
        media = p.get('media') if isinstance(p.get('media'), dict) else {}
        mime = media.get('mimetype') or ''
        if p.get('hasMedia'):
            mtype = {'image': 'image', 'video': 'video', 'audio': 'audio'}.get(mime.split('/')[0], 'document')
        else:
            mtype = 'text' if p.get('body') else 'other'
        message = Message.create({
            'conversation_id': conv.id, 'remote_message_id': remote_id,
            'direction': 'out' if from_me else 'in', 'message_type': mtype, 'body': p.get('body') or False,
            'date': ts_to_datetime(p.get('timestamp')),
            'delivery_status': ACK_MAP.get(p.get('ack'), 'sent') if from_me else False,
            'sender_identifier': (session.phone_number if from_me else (p.get('participant') or p.get('from'))),
            'webhook_event_id': self.id,
        })
        if p.get('hasMedia') and media.get('url'):
            self._store_media(session, message, media, mtype)
        if not from_me:
            conv._after_incoming(message)
        return 'done'

    MEDIA_MAX_BYTES = 20 * 1024 * 1024

    def _store_media(self, session, message, media, mtype):
        """Keep inbound/outbound media in Odoo. A failed download never loses the message itself."""
        try:
            raw = session.server_id._get_client().download(media['url'], max_bytes=self.MEDIA_MAX_BYTES)
        except WahaError as e:
            message.failure_reason = f"Media not downloaded: {e}"
            return
        mime = (media.get('mimetype') or '').split(';')[0] or 'application/octet-stream'
        extension = mimetypes.guess_extension(mime) or ''
        self.env['ir.attachment'].sudo().create({
            'name': media.get('filename') or f"{mtype}{extension}", 'raw': raw, 'mimetype': mime,
            'res_model': 'elblasy.waha.message', 'res_id': message.id,
        })

    def _handle_ack(self, session, p):
        remote_id = p.get('id')
        status = ACK_MAP.get(p.get('ack')) or ACK_NAME_MAP.get(str(p.get('ackName')).upper())
        if not remote_id or not status:
            return 'ignored'
        message = self.env['elblasy.waha.message'].sudo().search(
            [('session_id', '=', session.id), ('remote_message_id', '=', remote_id)], limit=1)
        if not message:
            # the ack may overtake the message (or our send response): retry later
            raise LookupError("message not yet known")
        message._set_ack(status)
        return 'done'

    @api.model
    def _cron_retry_failed(self):
        """Process new events in arrival order, retry failed ones after a short delay, then purge."""
        Cron = self.env['ir.cron']
        now = fields.Datetime.now()
        events = self.search([('state', '=', 'pending')], order='id', limit=BATCH_SIZE)
        events |= self.search([('state', '=', 'failed'), ('attempts', '<', MAX_ATTEMPTS),
                               ('write_date', '<', now - RETRY_DELAY)], order='id', limit=200)
        events = events.sorted('id')
        Cron._commit_progress(remaining=len(events))
        for event in events:
            event._process_event()
            if not Cron._commit_progress(1):
                break  # out of time: the cron runs again for the rest
        if len(events) < BATCH_SIZE:
            self.search([('state', 'in', ('done', 'ignored')), ('create_date', '<', now - timedelta(days=14))],
                        limit=1000).unlink()
            # failed events stay a month for investigation, then go (raw payloads may contain message content)
            self.search([('state', '=', 'failed'), ('create_date', '<', now - timedelta(days=30))],
                        limit=1000).unlink()
        elif self.search_count([('state', '=', 'pending')], limit=1):
            self.env.ref('elblasy_waha_connector.cron_waha_retry_events')._trigger()

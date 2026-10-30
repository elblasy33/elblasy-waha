import hmac
import json
import logging
from hashlib import sha512

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class WahaWebhook(http.Controller):

    @http.route('/waha/webhook/<string:token>', type='http', auth='public', methods=['POST'],
                csrf=False, save_session=False)
    def webhook(self, token, **_kw):
        raw = request.httprequest.get_data()
        session = request.env['elblasy.waha.session'].sudo().search([('webhook_token', '=', token)], limit=1)
        if not session:
            return request.make_json_response({'error': 'not found'}, status=404)
        server = session.server_id
        if server.use_hmac:
            expected = hmac.new((server.webhook_secret or '').encode(), raw, sha512).hexdigest()
            received = request.httprequest.headers.get('X-Webhook-Hmac', '')
            if not server.webhook_secret or not hmac.compare_digest(expected, received):
                _logger.warning("WAHA webhook rejected (bad signature) for session %s", session.id)
                return request.make_json_response({'error': 'forbidden'}, status=403)
        try:
            data = json.loads(raw)
        except ValueError:
            return request.make_json_response({'error': 'invalid json'}, status=400)
        if not isinstance(data, dict) or not isinstance(data.get('event'), str) \
                or data.get('session') != session.session_name:
            return request.make_json_response({'error': 'invalid payload'}, status=400)
        event = request.env['elblasy.waha.webhook.event'].sudo()._store_event(session, raw, data)
        if event:
            # answer WAHA at once; media downloads, auto-replies and the bridge run in the cron worker
            event._schedule_processing()
        return request.make_json_response({'ok': True})

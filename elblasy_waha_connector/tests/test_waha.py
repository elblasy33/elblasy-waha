import hashlib
import hmac
import json
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.elblasy_waha_connector.models.waha_client import WahaClient, WahaError

CLIENT = 'odoo.addons.elblasy_waha_connector.models.waha_client.WahaClient'


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data
        self.content = b'x' if data is not None else b''

    def json(self):
        return self._data


class WahaCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.user = Users.create({'name': 'W User', 'login': 'wuser', 'email': 'wuser@example.com',
                                 'group_ids': [(6, 0, [cls.env.ref('elblasy_waha_connector.group_waha_user').id])]})
        cls.manager = Users.create({'name': 'W Mgr', 'login': 'wmgr', 'email': 'wmgr@example.com',
                                    'group_ids': [(6, 0, [cls.env.ref('elblasy_waha_connector.group_waha_manager').id])]})
        cls.server = cls.env['elblasy.waha.server'].create({
            'name': 'S', 'base_url': 'http://waha.test', 'api_key': 'SECRETKEY', 'webhook_secret': 'hooksecret'})
        cls.session = cls.env['elblasy.waha.session'].create({
            'name': 'Sales', 'session_name': 'sales', 'server_id': cls.server.id})

    def _event(self, etype, payload, evt_id='evt_1', session='sales', **extra):
        return {'id': evt_id, 'event': etype, 'session': session, 'payload': payload, **extra}

    def _incoming(self, mid='false_201001234567@c.us_AAA', body='Hello', chat='201001234567@c.us'):
        return self._event('message.any', {
            'id': mid, 'timestamp': 1700000000, 'from': chat, 'fromMe': False, 'body': body, 'hasMedia': False,
            '_data': {'notifyName': 'Ahmed'}}, evt_id='evt_' + mid)

    def _feed(self, data):
        raw = json.dumps(data).encode()
        event = self.env['elblasy.waha.webhook.event']._store_event(self.session, raw, data)
        event._process_event()
        return event


class TestClient(TransactionCase):

    def _client(self):
        return WahaClient('http://waha.test', 'SECRETKEY')

    def test_01_success(self):
        with patch('requests.request', return_value=FakeResponse(200, [])) as req:
            self.assertEqual(self._client().list_sessions(), [])
            self.assertEqual(req.call_args.kwargs['headers']['X-Api-Key'], 'SECRETKEY')

    def test_02_bad_api_key(self):
        with patch('requests.request', return_value=FakeResponse(401, {})):
            with self.assertRaises(WahaError) as cm:
                self._client().list_sessions()
        self.assertEqual(cm.exception.kind, 'auth')

    def test_03_unreachable_no_secret_leak(self):
        import requests
        with patch('requests.request', side_effect=requests.exceptions.ConnectionError('http://waha.test X-Api-Key SECRETKEY')):
            with self.assertRaises(WahaError) as cm:
                self._client().list_sessions()
        self.assertEqual(cm.exception.kind, 'network')
        self.assertNotIn('SECRETKEY', str(cm.exception))

    def test_04_non_json(self):
        class Bad(FakeResponse):
            def json(self):
                raise ValueError
        with patch('requests.request', return_value=Bad(200, 'x')):
            with self.assertRaises(WahaError) as cm:
                self._client().list_sessions()
        self.assertEqual(cm.exception.kind, 'response')


@tagged('post_install', '-at_install')
class TestWaha(WahaCommon):

    def test_01_test_connection(self):
        with patch(f'{CLIENT}.list_sessions', return_value=[]):
            res = self.server.with_user(self.manager).action_test_connection()
        self.assertEqual(res['params']['type'], 'success')
        with patch(f'{CLIENT}.list_sessions', side_effect=WahaError('auth', 'WAHA rejected the API key.')):
            res = self.server.with_user(self.manager).action_test_connection()
        self.assertEqual(res['params']['type'], 'danger')
        self.assertIn('Authentication', res['params']['message'])
        with self.assertRaises(AccessError):
            self.server.with_user(self.user).action_test_connection()

    def test_02_create_session_idempotent(self):
        calls = []
        with patch(f'{CLIENT}.get_session', side_effect=[WahaError('not_found', 'x', 404),
                                                          {'status': 'SCAN_QR_CODE'}]), \
             patch(f'{CLIENT}.create_session', side_effect=lambda *a, **k: calls.append(a) or {'status': 'STOPPED'}), \
             patch(f'{CLIENT}.start_session'), \
             patch(f'{CLIENT}.get_qr', return_value={'mimetype': 'image/png', 'data': 'QUJD'}):
            act = self.session.with_user(self.manager).action_connect()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]['webhooks'][0]['events'], self.session._webhook_events())
        self.assertIn('label.chat.added', self.session._webhook_events())
        self.assertNotIn('call.received', self.session._webhook_events())
        self.assertEqual(self.session.status, 'SCAN_QR_CODE')
        self.assertTrue(self.session.qr_image)
        self.assertEqual(act['target'], 'new')

    def test_03_status_and_stopped_after_connected(self):
        self.session._apply_remote({'status': 'WORKING', 'me': {'id': '201001234567@c.us', 'pushName': 'Shop'}})
        self.assertEqual((self.session.status, self.session.phone_number, self.session.whatsapp_profile_name),
                         ('WORKING', '201001234567', 'Shop'))
        self.assertFalse(self.session.qr_image)
        self.assertTrue(self.session.last_connected_at)
        with patch(f'{CLIENT}.get_session', return_value={'status': 'STOPPED'}):
            self.session.with_user(self.manager).action_refresh_status()
        self.assertEqual(self.session.status, 'STOPPED')

    def test_04_unknown_status_kept_raw(self):
        self.session._apply_remote({'status': 'SOMETHING_NEW'})
        self.assertEqual(self.session.status_raw, 'SOMETHING_NEW')

    def test_05_incoming_and_dedupe(self):
        data = self._incoming()
        self._feed(data)
        self._feed(data)  # same event id
        self._feed(self._event('message.any', data['payload'], evt_id='evt_other'))  # same message, new event
        msgs = self.env['elblasy.waha.message'].search([('session_id', '=', self.session.id)])
        self.assertEqual(len(msgs), 1)
        conv = msgs.conversation_id
        self.assertEqual((conv.unread_count, conv.remote_phone, conv.display_name_remote),
                         (1, '201001234567', 'Ahmed'))
        self.assertEqual(conv.last_message_preview, 'Hello')

    def test_06_invalid_event_does_not_block(self):
        bad = self._event('message.any', {'foo': 'bar'}, evt_id='bad')
        ev = self._feed(bad)
        self.assertEqual(ev.state, 'failed')
        ok = self._feed(self._incoming())
        self.assertEqual(ok.state, 'done')

    def test_07_group_ignored(self):
        ev = self._feed(self._incoming(chat='1203@g.us', mid='g1'))
        self.assertEqual(ev.state, 'ignored')

    def test_08_send_text(self):
        self._feed(self._incoming())
        conv = self.env['elblasy.waha.conversation'].search([('session_id', '=', self.session.id)])
        self.session._apply_remote({'status': 'WORKING'})
        with patch(f'{CLIENT}.send_text', return_value={'id': 'true_201001234567@c.us_BBB'}) as send:
            res = conv.with_user(self.user).inbox_send_text('Hi', 'tok1')
            again = conv.with_user(self.user).inbox_send_text('Hi', 'tok1')
        self.assertEqual(send.call_count, 1)
        self.assertEqual(res['id'], again['id'])
        self.assertEqual(res['status'], 'sent')
        # webhook echo + ack must not duplicate
        echo = self._event('message.any', {'id': 'true_201001234567@c.us_BBB', 'timestamp': 1700000100,
                                           'from': '999@c.us', 'to': '201001234567@c.us', 'fromMe': True,
                                           'body': 'Hi', 'ack': 1}, evt_id='evt_echo')
        self._feed(echo)
        self._feed(self._event('message.ack', {'id': 'true_201001234567@c.us_BBB', 'ack': 3}, evt_id='evt_ack'))
        out = self.env['elblasy.waha.message'].search([('direction', '=', 'out'), ('session_id', '=', self.session.id)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out.delivery_status, 'read')
        # late lower ack never downgrades
        self._feed(self._event('message.ack', {'id': 'true_201001234567@c.us_BBB', 'ack': 1}, evt_id='evt_ack2'))
        self.assertEqual(out.delivery_status, 'read')

    def test_09_send_failures(self):
        self._feed(self._incoming())
        conv = self.env['elblasy.waha.conversation'].search([('session_id', '=', self.session.id)])
        with self.assertRaises(UserError):  # session not connected
            conv.with_user(self.user).inbox_send_text('Hi', 't0')
        self.session._apply_remote({'status': 'WORKING'})
        with patch(f'{CLIENT}.send_text', side_effect=WahaError('network', 'timeout')):
            res = conv.with_user(self.user).inbox_send_text('Hi', 't1')
        self.assertEqual(res['status'], 'unknown')  # ambiguous: never auto-retried
        with patch(f'{CLIENT}.send_text', side_effect=WahaError('http', 'HTTP 422', 422)):
            res = conv.with_user(self.user).inbox_send_text('Hi', 't2')
        self.assertEqual(res['status'], 'failed')

    def test_10_partner_link(self):
        partner = self.env['res.partner'].create({'name': 'Ahmed A', 'phone': '+20 100 123 4567'})
        self.env['res.partner'].create({'name': 'Ahmed B', 'phone': '0100 765 4321'})
        self._feed(self._incoming())
        conv = self.env['elblasy.waha.conversation'].search([('session_id', '=', self.session.id)])
        self.assertEqual(conv.partner_id, partner)
        other = self.env['elblasy.waha.conversation']._get_or_create(self.session, '49170999999@c.us', 'Hans')
        self.assertFalse(other.partner_id)
        pid = other.action_create_partner()
        self.assertEqual(other.partner_id.id, pid)
        with patch(f'{CLIENT}.lid_to_phone', return_value=False):
            lid = self.env['elblasy.waha.conversation']._get_or_create(self.session, '123@lid')
        self.assertFalse(lid.remote_phone)
        self.assertEqual(lid.chat_type, 'lid')

    def test_10b_manual_conversation(self):
        Conv = self.env['elblasy.waha.conversation']
        conv = Conv.create({'session_id': self.session.id, 'remote_phone': '+20 100 111 2222'})
        self.assertEqual(conv.remote_chat_id, '201001112222@c.us')
        partner = self.env['res.partner'].create({'name': 'P', 'phone': '0049 170 55 55 55'})
        conv2 = Conv.create({'session_id': self.session.id, 'partner_id': partner.id})
        self.assertEqual(conv2.remote_chat_id, '49170555555@c.us')
        with self.assertRaises(UserError):
            Conv.create({'session_id': self.session.id})

    def test_11_permissions(self):
        self._feed(self._incoming())
        with self.assertRaises(AccessError):
            self.server.with_user(self.user).api_key
        with self.assertRaises(AccessError):
            self.session.with_user(self.user).action_restart()
        with self.assertRaises(UserError):
            self.session.with_user(self.manager).action_logout()
        outsider = self.env['res.users'].with_context(no_reset_password=True).create(
            {'name': 'Out', 'login': 'out', 'email': 'o@example.com'})
        with self.assertRaises(AccessError):
            self.env['elblasy.waha.conversation'].with_user(outsider).inbox_conversations()

    def test_12_multi_company(self):
        company2 = self.env['res.company'].create({'name': 'C2'})
        server2 = self.env['elblasy.waha.server'].create({'name': 'S2', 'base_url': 'http://x.test', 'company_id': company2.id})
        s2 = self.env['elblasy.waha.session'].create({'name': 'S2', 'session_name': 's2', 'server_id': server2.id})
        visible = self.env['elblasy.waha.session'].with_user(self.user).search([])
        self.assertIn(self.session, visible)
        self.assertNotIn(s2, visible)

    def test_13_inbox_lists(self):
        self._feed(self._incoming())
        res = self.env['elblasy.waha.conversation'].with_user(self.user).inbox_conversations(session_id=self.session.id)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['unread'], 1)
        conv = self.env['elblasy.waha.conversation'].browse(res[0]['id'])
        msgs = conv.with_user(self.user).inbox_messages()
        self.assertEqual(msgs['messages'][0]['body'], 'Hello')
        with patch(f'{CLIENT}.post', return_value={}):
            conv.with_user(self.user).inbox_mark_read()
        self.assertEqual(conv.unread_count, 0)

    def test_14_session_status_event(self):
        ev = self._feed(self._event('session.status', {'status': 'WORKING'}, me={'id': '2010@c.us', 'pushName': 'X'}))
        self.assertEqual(ev.state, 'done')
        self.assertEqual(self.session.status, 'WORKING')


@tagged('post_install', '-at_install')
class TestWebhookHttp(WahaCommon, HttpCase):

    def _post(self, data, secret='hooksecret', token=None, raw=None):
        raw = raw if raw is not None else json.dumps(data).encode()
        headers = {'Content-Type': 'application/json',
                   'X-Webhook-Hmac': hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest()}
        url = f'/waha/webhook/{token or self.session.webhook_token}'
        return self.url_open(url, data=raw, headers=headers)

    def test_01_webhook(self):
        self.assertEqual(self._post(self._incoming()).status_code, 200)
        self.assertEqual(self._post(self._incoming()).status_code, 200)  # replay
        self.assertEqual(self.env['elblasy.waha.message'].search_count([('session_id', '=', self.session.id)]), 1)

    def test_02_webhook_rejections(self):
        self.assertEqual(self._post(self._incoming(), secret='wrong').status_code, 403)
        self.assertEqual(self._post(self._incoming(), token='nope').status_code, 404)
        self.assertEqual(self._post({}, raw=b'not json').status_code, 400)
        self.assertEqual(self._post({'event': 'message.any', 'session': 'other', 'payload': {}}).status_code, 400)

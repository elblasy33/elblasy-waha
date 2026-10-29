import base64
import json
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.elblasy_waha_connector.models.waha_client import WahaError
from odoo.addons.whatsapp.tools.whatsapp_api import WhatsAppApi
from odoo.addons.whatsapp.tools.whatsapp_exception import WhatsAppError

CLIENT = 'odoo.addons.elblasy_waha_connector.models.waha_client.WahaClient'


@tagged('post_install', '-at_install')
class TestBridge(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'WA Admin', 'login': 'waadmin', 'email': 'waadmin@example.com',
            'group_ids': [(6, 0, [cls.env.ref('whatsapp.group_whatsapp_admin').id,
                                  cls.env.ref('elblasy_waha_connector.group_waha_admin').id])]})
        cls.server = cls.env['elblasy.waha.server'].create({'name': 'S', 'base_url': 'http://waha.test', 'api_key': 'K'})
        cls.session = cls.env['elblasy.waha.session'].create({
            'name': 'Main', 'session_name': 'main', 'server_id': cls.server.id, 'phone_number': '201000000000'})
        with patch(f'{CLIENT}.get_session', side_effect=WahaError('not_found', 'x', 404)):
            cls.session.with_user(cls.admin).action_enable_odoo_whatsapp()
        cls.account = cls.session._get_whatsapp_account()

    def _api(self):
        return WhatsAppApi(self.account)

    def _feed(self, data, key):
        data = {'id': key, 'session': 'main', **data}
        raw = json.dumps(data).encode()
        event = self.env['elblasy.waha.webhook.event']._store_event(self.session, raw, data)
        event._process_event()
        return event

    def test_01_enable_idempotent(self):
        self.assertTrue(self.account)
        self.assertEqual(self.account.waha_session_id, self.session)
        self.assertTrue(self.session.odoo_whatsapp_enabled)
        with patch(f'{CLIENT}.get_session', side_effect=WahaError('not_found', 'x', 404)):
            self.session.with_user(self.admin).action_enable_odoo_whatsapp()
        self.assertEqual(self.env['whatsapp.account'].search_count([('waha_session_id', '=', self.session.id)]), 1)
        self.assertIn('message.reaction', self.session._webhook_config()['webhooks'][0]['events'])
        with self.assertRaises(AccessError):
            self.session.action_enable_odoo_whatsapp()  # superuser env has no whatsapp admin group check bypass? see below

    def test_02_send_text(self):
        with patch(f'{CLIENT}.post', return_value={'id': 'true_2010@c.us_X'}) as post:
            uid = self._api()._send_whatsapp('+20 100 123 4567', 'text', {'body': 'hi', 'preview_url': True},
                                             parent_message_id='false_2010@c.us_P')
        self.assertEqual(uid, 'true_2010@c.us_X')
        path, payload = post.call_args.args
        self.assertEqual(path, '/api/sendText')
        self.assertEqual((payload['chatId'], payload['text'], payload['reply_to'], payload['session']),
                         ('201001234567@c.us', 'hi', 'false_2010@c.us_P', 'main'))

    def test_03_send_failures(self):
        with patch(f'{CLIENT}.post', side_effect=WahaError('network', 'timeout')):
            with self.assertRaises(WhatsAppError) as cm:
                self._api()._send_whatsapp('201001234567', 'text', {'body': 'hi'})
        self.assertEqual(cm.exception.failure_type, 'network')
        with patch(f'{CLIENT}.post', return_value={}):
            with self.assertRaises(WhatsAppError):
                self._api()._send_whatsapp('201001234567', 'text', {'body': 'hi'})

    def test_04_send_media(self):
        attachment = self.env['ir.attachment'].create({'name': 'a.png', 'raw': b'PNGDATA', 'mimetype': 'image/png'})
        media_id = self._api()._upload_whatsapp_document(attachment)
        with patch(f'{CLIENT}.post', return_value={'id': 'true_2010@c.us_M'}) as post:
            self._api()._send_whatsapp('201001234567', 'image', {'id': media_id, 'caption': 'cap'})
        path, payload = post.call_args.args
        self.assertEqual(path, '/api/sendImage')
        self.assertEqual(base64.b64decode(payload['file']['data']), b'PNGDATA')
        self.assertEqual(payload['caption'], 'cap')

    def test_04b_template_header_document(self):
        attachment = self.env['ir.attachment'].create({'name': 'o.pdf', 'raw': b'%PDF', 'mimetype': 'application/pdf'})
        media_id = self._api()._upload_whatsapp_document(attachment)
        self.env['whatsapp.template'].create({
            'name': 'T', 'template_name': 'tplx', 'wa_account_id': self.account.id, 'body': 'Hi {{1}}',
            'model_id': self.env['ir.model']._get('res.partner').id, 'template_type': 'utility'})
        send_vals = {'name': 'tplx', 'language': {'code': 'en'}, 'components': [
            {'type': 'header', 'parameters': [{'type': 'document', 'document': {'id': media_id, 'filename': 'o.pdf'}}]},
            {'type': 'body', 'parameters': [{'type': 'text', 'text': 'Ahmed'}]}]}
        with patch(f'{CLIENT}.post', return_value={'id': 'true_t'}) as post:
            self._api()._send_whatsapp('201001234567', 'template', send_vals)
        path, payload = post.call_args.args
        self.assertEqual(path, '/api/sendFile')
        self.assertEqual(payload['caption'], 'Hi Ahmed')
        self.assertEqual(base64.b64decode(payload['file']['data']), b'%PDF')
        self.assertEqual(payload['file']['filename'], 'o.pdf')

    def test_05_reaction_and_interactive(self):
        with patch(f'{CLIENT}.put', return_value={}) as put:
            self._api()._send_whatsapp('201001234567', 'reaction', {'message_id': 'm1', 'emoji': '👍'})
        self.assertEqual(put.call_args.args[1]['reaction'], '👍')
        with patch(f'{CLIENT}.post', return_value={'id': 'true_i'}) as post:
            self._api()._send_whatsapp('201001234567', 'interactive', {
                'type': 'button', 'body': {'text': 'Pick'},
                'action': {'buttons': [{'reply': {'id': '1', 'title': 'Yes'}}, {'reply': {'id': '2', 'title': 'No'}}]}})
        self.assertEqual(post.call_args.args[1]['text'], 'Pick\n\n1. Yes\n2. No')

    def test_06_inbound_text_and_dedupe(self):
        payload = {'id': 'false_201001234567@c.us_IN1', 'timestamp': 1700000000, 'from': '201001234567@c.us',
                   'fromMe': False, 'body': 'Hello Odoo', 'hasMedia': False, '_data': {'notifyName': 'Ahmed'}}
        ev = self._feed({'event': 'message.any', 'payload': payload}, 'e1')
        self.assertEqual(ev.state, 'done')
        self._feed({'event': 'message.any', 'payload': payload}, 'e1')  # same event
        channels = self.env['discuss.channel'].search([('channel_type', '=', 'whatsapp'), ('wa_account_id', '=', self.account.id)])
        self.assertEqual(len(channels), 1)
        self.assertEqual(len(channels.message_ids.filtered(lambda m: 'Hello Odoo' in (m.body or ''))), 1)
        # the custom inbox is bypassed for a bridged session
        self.assertFalse(self.env['elblasy.waha.message'].search([('session_id', '=', self.session.id)]))

    def test_06b_inbound_lid_resolved_and_unresolved(self):
        base = {'timestamp': 1700000005, 'fromMe': False, 'hasMedia': False, '_data': {'notifyName': 'Lida'}}
        with patch(f'{CLIENT}.lid_to_phone', return_value='201007770001') as lid:
            ev = self._feed({'event': 'message.any', 'payload': {
                **base, 'id': 'false_lid1', 'from': '999111@lid', 'body': 'via lid'}}, 'l1')
        self.assertEqual(ev.state, 'done')
        lid.assert_called_once()
        self.assertTrue(self.env['discuss.channel'].search([('channel_type', '=', 'whatsapp'), ('whatsapp_number', 'like', '1007770001')]))
        with patch(f'{CLIENT}.lid_to_phone', return_value=False):
            ev = self._feed({'event': 'message.any', 'payload': {
                **base, 'id': 'false_lid2', 'from': '888222@lid', 'body': 'unknown lid'}}, 'l2')
        self.assertEqual(ev.state, 'done')  # handled through the BSUID, no phone
        message = self.env['mail.message'].search([('body', 'like', 'unknown lid'), ('model', '=', 'discuss.channel')])
        self.assertEqual(len(message), 1)  # reached a channel through the BSUID path

    def test_06c_sync_messages_idempotent(self):
        chat = {'id': '201001234567@c.us'}
        msg = {'id': 'false_201001234567@c.us_SYNC1', 'timestamp': 4102444800, 'from': '201001234567@c.us',
               'fromMe': False, 'body': 'from sync', 'hasMedia': False}

        def fake_get(self_, path, params=None):
            return [chat] if path.endswith('/chats/overview') else [dict(msg)]
        with patch(f'{CLIENT}.get', fake_get):
            self.session.with_user(self.admin).action_sync_messages()
            self.session.messages_synced_at = False
            self.session.with_user(self.admin).action_sync_messages()  # again: no duplicate
            # a later webhook for the same message is ignored as well
            ev = self._feed({'event': 'message.any', 'payload': dict(msg, id=msg['id'])}, 'w-sync')
        self.assertEqual(ev.state, 'ignored')
        channel = self.env['discuss.channel'].search([('channel_type', '=', 'whatsapp'), ('wa_account_id', '=', self.account.id)])
        self.assertEqual(len(channel.message_ids.filtered(lambda m: 'from sync' in (m.body or ''))), 1)

    def test_07_inbound_media(self):
        payload = {'id': 'false_201001234567@c.us_IN2', 'timestamp': 1700000001, 'from': '201001234567@c.us',
                   'fromMe': False, 'body': 'look', 'hasMedia': True,
                   'media': {'url': 'http://internal:3000/api/files/x.jpg', 'mimetype': 'image/jpeg', 'filename': 'x.jpg'}}
        with patch(f'{CLIENT}.download', return_value=b'JPEG') as dl:
            ev = self._feed({'event': 'message.any', 'payload': payload}, 'e2')
        self.assertEqual(ev.state, 'done')
        dl.assert_called_once()
        attachments = self.env['ir.attachment'].search([('name', '=', 'x.jpg')])
        self.assertTrue(attachments)

    def test_08_ack_and_out_of_order(self):
        ev = self._feed({'event': 'message.ack', 'payload': {'id': 'true_zz', 'ack': 3}}, 'a0')
        self.assertEqual(ev.state, 'failed')  # unknown yet: retried later
        msg = self.env['whatsapp.message'].create({
            'mobile_number': '201001234567', 'msg_uid': 'true_zz', 'wa_account_id': self.account.id, 'state': 'sent'})
        ev._process_event()
        self.assertEqual(ev.state, 'done')
        self.assertEqual(msg.state, 'read')

    def test_09_meta_account_untouched(self):
        plain = self.env['whatsapp.account'].create({
            'name': 'Meta', 'app_uid': 'a', 'account_uid': 'b', 'phone_uid': 'c', 'app_secret': 's', 'token': 't',
            'notify_user_ids': [(6, 0, self.admin.ids)]})
        with self.assertRaises(WhatsAppError):
            WhatsAppApi(plain)._send_whatsapp('201001234567', 'text', {'body': 'hi'})  # original path: requests disabled in tests

    def test_10_disable(self):
        self.session.with_user(self.admin).action_disable_odoo_whatsapp()
        self.assertFalse(self.session.odoo_whatsapp_enabled)

    # ------------------------------------------------------------------ message anyone from Discuss
    def test_11_start_chat_with_anyone_and_free_text(self):
        user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Agent', 'login': 'agent1', 'email': 'agent1@example.com',
            'group_ids': [(6, 0, [self.env.ref('elblasy_waha_connector.group_waha_user').id])]})
        partner = self.env['res.partner'].create({'name': 'Brand New', 'phone': '+20 100 321 0987'})
        wiz = self.env['elblasy.waha.new.chat'].with_user(user).with_context(
            active_model='res.partner', active_id=partner.id).create({'message': 'Hello from Odoo'})
        self.assertEqual((wiz.account_id, wiz.phone), (self.account, '+20 100 321 0987'))
        with patch(f'{CLIENT}.post', return_value={'id': 'true_nc1'}) as post:
            action = wiz.action_start()
        self.assertIn('discuss.channel_', action['url'])
        channel = self.env['discuss.channel'].browse(int(action['url'].rsplit('_', 1)[1]))
        self.assertEqual(channel.channel_type, 'whatsapp')
        self.assertIn(user.partner_id, channel.channel_member_ids.partner_id)
        # no template, no inbound message yet, yet the conversation is open (no 24h rule with WAHA)
        self.assertTrue(channel.whatsapp_channel_valid_until > fields.Datetime.now())
        self.assertTrue(channel.whatsapp_channel_active)
        self.assertEqual(post.call_args.args[0], '/api/sendText')
        self.assertEqual(post.call_args.args[1]['chatId'], '201003210987@c.us')
        self.assertEqual(post.call_args.args[1]['text'], 'Hello from Odoo')
        # a second start re-uses the same channel and can post again freely
        with patch(f'{CLIENT}.post', return_value={'id': 'true_nc2'}):
            again = self.account._waha_start_chat(number='+201003210987', first_message='Second')
        self.assertEqual(again, channel)

    def test_12_start_chat_needs_valid_number(self):
        with self.assertRaises(UserError):
            self.account._waha_start_chat(number='123')

    # ------------------------------------------------------------------ native composer without templates
    def test_13_composer_free_text_without_template(self):
        partner = self.env['res.partner'].create({'name': 'No Template', 'phone': '+20 100 654 0001'})
        Composer = self.env['whatsapp.composer'].with_context(active_model='res.partner', active_id=partner.id)
        defaults = Composer.default_get(['res_model', 'wa_template_id', 'res_ids'])  # must not raise "no template"
        self.assertEqual(defaults['res_model'], 'res.partner')
        composer = Composer.create({**defaults, 'waha_message': 'Quick hello'})
        self.assertTrue(composer.waha_free_text)
        self.assertIn('201006540001', ''.join(c for c in composer.phone if c.isdigit()))
        with patch(f'{CLIENT}.post', return_value={'id': 'true_ft1'}) as post:
            res = composer.action_send_whatsapp_template()
        self.assertEqual(res['params']['type'], 'success')
        self.assertEqual(post.call_args.args[1]['text'], 'Quick hello')
        self.assertEqual(post.call_args.args[1]['chatId'], '201006540001@c.us')
        self.assertTrue(partner.message_ids.filtered(lambda m: 'WhatsApp message sent' in (m.body or '')))

    def test_14_composer_free_text_guards(self):
        partner = self.env['res.partner'].create({'name': 'Guard', 'phone': '+20 100 654 0002'})
        Composer = self.env['whatsapp.composer'].with_context(active_model='res.partner', active_id=partner.id)
        composer = Composer.create({**Composer.default_get(['res_model', 'res_ids'])})
        with self.assertRaises(ValidationError):  # empty message
            composer.action_send_whatsapp_template()
        # without any WAHA account the native behaviour (template required) is untouched
        self.account.waha_session_id = False
        with self.assertRaises((UserError, ValidationError)):
            Composer.default_get(['res_model', 'wa_template_id'])

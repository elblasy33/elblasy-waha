from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.elblasy_waha_connector.models.waha_client import WahaError

CLIENT = 'odoo.addons.elblasy_waha_connector.models.waha_client.WahaClient'


@tagged('post_install', '-at_install')
class TestGroups(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.user = Users.create({'name': 'U', 'login': 'gu', 'email': 'gu@example.com',
                                 'group_ids': [(6, 0, [cls.env.ref('elblasy_waha_connector.group_waha_user').id])]})
        cls.manager = Users.create({'name': 'M', 'login': 'gm', 'email': 'gm@example.com',
                                    'group_ids': [(6, 0, [cls.env.ref('elblasy_waha_connector.group_waha_manager').id])]})
        cls.server = cls.env['elblasy.waha.server'].create({'name': 'S', 'base_url': 'http://waha.test', 'api_key': 'K'})
        cls.session = cls.env['elblasy.waha.session'].create({
            'name': 'Main', 'session_name': 'main', 'server_id': cls.server.id, 'status': 'WORKING'})

    def setUp(self):
        super().setUp()
        patcher = patch(f'{CLIENT}.check_exists', return_value=(True, None))  # number check is tested separately
        self.check_exists = patcher.start()
        self.addCleanup(patcher.stop)

    def _sync(self, groups):
        with patch(f'{CLIENT}.get', return_value=groups):
            return self.session.with_user(self.manager).action_sync_groups()

    def test_01_sync_groups(self):
        self._sync([
            {'id': '1203@g.us', 'subject': 'Sales team', 'participants': [{}, {}, {}]},
            {'id': {'_serialized': '1204@g.us'}, 'name': 'Ops'},
            {'id': '555@c.us', 'subject': 'not a group'},
        ])
        groups = self.env['elblasy.waha.group'].search([('session_id', '=', self.session.id)])
        self.assertEqual(sorted(groups.mapped('name')), ['Ops', 'Sales team'])
        self.assertEqual(groups.filtered(lambda g: g.name == 'Sales team').participants_count, 3)
        self.assertFalse(any(groups.mapped('allow_send')))  # whitelist is opt-in
        with self.assertRaises(UserError):  # WAHA rate-limits group listing: one sync per minute
            self._sync([{'id': '1203@g.us', 'subject': 'Sales renamed'}])
        self.session.groups_synced_at = False
        self._sync([{'id': '1203@g.us', 'subject': 'Sales renamed'}])  # idempotent, updates
        self.assertEqual(len(self.env['elblasy.waha.group'].search([('session_id', '=', self.session.id)])), 2)
        self.assertIn('Sales renamed', self.env['elblasy.waha.group'].search([]).mapped('name'))

    def test_02_sync_requires_manager(self):
        with self.assertRaises(AccessError):
            self.session.with_user(self.user).action_sync_groups()

    def _group(self, allow=True):
        return self.env['elblasy.waha.group'].create({
            'name': 'G', 'session_id': self.session.id, 'chat_id': '1203@g.us', 'allow_send': allow})

    def test_03_send_text(self):
        group = self._group()
        with patch(f'{CLIENT}.post', return_value={'id': 'true_1203@g.us_A'}) as post:
            post_rec = self.env['elblasy.waha.group.post'].with_user(self.user).send_to_group(group, 'Hello team')
        path, payload = post.call_args.args
        self.assertEqual((path, payload['chatId'], payload['text']), ('/api/sendText', '1203@g.us', 'Hello team'))
        self.assertEqual((post_rec.state, post_rec.wa_message_ids), ('sent', 'true_1203@g.us_A'))

    def test_04_send_document_with_caption_and_chatter(self):
        group = self._group()
        partner = self.env['res.partner'].create({'name': 'Doc owner'})
        att = self.env['ir.attachment'].with_user(self.user).create({'name': 'inv.pdf', 'raw': b'%PDF-1.4 sample', 'mimetype': 'application/pdf'})
        with patch(f'{CLIENT}.post', return_value={'id': 'true_B'}) as post:
            rec = self.env['elblasy.waha.group.post'].with_user(self.user).send_to_group(
                group, 'Invoice attached', att, 'res.partner', partner.id)
        path, payload = post.call_args.args
        self.assertEqual(path, '/api/sendFile')
        self.assertEqual((payload['caption'], payload['file']['filename']), ('Invoice attached', 'inv.pdf'))
        self.assertEqual(rec.state, 'sent')
        self.assertTrue(partner.message_ids.filtered(lambda m: 'WhatsApp message to' in (m.body or '')))

    def test_05_guards(self):
        Post = self.env['elblasy.waha.group.post'].with_user(self.user)
        with self.assertRaises(UserError):  # not whitelisted
            Post.send_to_group(self._group(allow=False), 'x')
        group = self._group()
        with self.assertRaises(UserError):  # empty
            Post.send_to_group(group, '  ')
        self.session.status = 'STOPPED'
        with self.assertRaises(UserError):  # session down
            Post.send_to_group(group, 'x')
        outsider = self.env['res.users'].with_context(no_reset_password=True).create(
            {'name': 'O', 'login': 'go', 'email': 'go@example.com'})
        self.session.status = 'WORKING'
        with self.assertRaises(AccessError):
            self.env['elblasy.waha.group.post'].with_user(outsider).send_to_group(group, 'x')

    def test_06_failures_and_idempotency(self):
        group = self._group()
        Post = self.env['elblasy.waha.group.post'].with_user(self.user)
        with patch(f'{CLIENT}.post', side_effect=WahaError('network', 'timeout')):
            rec = Post.send_to_group(group, 'x', client_token='t1')
        self.assertEqual(rec.state, 'unknown')  # ambiguous: no automatic retry
        with patch(f'{CLIENT}.post') as post:
            again = Post.send_to_group(group, 'x', client_token='t1')
        post.assert_not_called()
        self.assertEqual(rec, again)
        with patch(f'{CLIENT}.post', side_effect=WahaError('http', 'HTTP 400', 400)):
            rec = Post.send_to_group(group, 'y', client_token='t2')
        self.assertEqual(rec.state, 'failed')

    def test_07_rate_limit(self):
        group = self._group()
        Post = self.env['elblasy.waha.group.post'].with_user(self.user)
        with patch(f'{CLIENT}.post', return_value={'id': 'true_x'}):
            for i in range(10):
                Post.send_to_group(group, f'm{i}')
            with self.assertRaises(UserError):
                Post.send_to_group(group, 'one too many')

    def test_08_wizard_customer_and_linked_group(self):
        group = self._group()
        other = self.env['elblasy.waha.group'].create({
            'name': 'Other', 'session_id': self.session.id, 'chat_id': '999@g.us', 'allow_send': True})
        partner = self.env['res.partner'].create({
            'name': 'Cust', 'phone': '+20 100 123 4567', 'waha_group_ids': [(6, 0, group.ids)]})
        wiz = self.env['elblasy.waha.group.send'].with_user(self.user).with_context(
            active_model='res.partner', active_id=partner.id).create({'body': 'Doc ready', 'session_id': self.session.id})
        self.assertEqual(wiz.partner_id, partner)
        self.assertEqual(wiz.group_ids, group)  # only the customer's own group is proposed
        self.assertTrue(wiz.send_to_customer)
        self.assertEqual(wiz.allowed_group_ids, group)
        self.assertNotIn(other, wiz.allowed_group_ids)
        with patch(f'{CLIENT}.post', return_value={'id': 'true_w'}) as post:
            res = wiz.action_send()
        self.assertEqual(res['params']['type'], 'success')
        chats = sorted(call.args[1]['chatId'] for call in post.call_args_list)
        self.assertEqual(chats, ['1203@g.us', '201001234567@c.us'])
        posts = self.env['elblasy.waha.group.post'].search([('session_id', '=', self.session.id)])
        self.assertEqual(sorted(posts.mapped('target_name')), ['Cust', 'G'])

    def test_09_wizard_needs_target_and_valid_phone(self):
        partner = self.env['res.partner'].create({'name': 'NoPhone'})
        wiz = self.env['elblasy.waha.group.send'].with_user(self.user).with_context(
            active_model='res.partner', active_id=partner.id).create({'body': 'x'})
        with self.assertRaises(UserError):  # nothing selected
            wiz.action_send()
        wiz.send_to_customer = True
        with self.assertRaises(UserError):  # customer without phone
            wiz.action_send()

    def test_10_instant_message_from_contact_and_employee(self):
        partner = self.env['res.partner'].create({'name': 'Quick', 'phone': '+20 100 555 0001'})
        action = partner.with_user(self.user).action_send_whatsapp()
        wiz = self.env['elblasy.waha.group.send'].with_user(self.user).with_context(**action['context']).create({
            'body': 'Hi now', 'session_id': self.session.id})
        self.assertEqual((wiz.phone, wiz.send_to_customer), ('+20 100 555 0001', True))
        with patch(f'{CLIENT}.post', return_value={'id': 'true_q'}) as post:
            wiz.action_send()
        self.assertEqual(post.call_args.args[1]['chatId'], '201005550001@c.us')
        # employee-like record: own mobile first, then the work contact
        employee = self.env['hr.employee'].create({'name': 'Emp', 'mobile_phone': '0020 111 222 3333'}) \
            if 'hr.employee' in self.env else None
        if employee:
            _partner, phone = self.env['elblasy.waha.group.post']._record_targets(employee)
            self.assertEqual(phone, '0020 111 222 3333')
            with patch(f'{CLIENT}.post', return_value={'id': 'true_e'}) as post:
                self.env['elblasy.waha.group.post'].with_user(self.user).send_to_number(
                    phone, self.session, 'hello', res_model='hr.employee', res_id=employee.id)
            self.assertEqual(post.call_args.args[1]['chatId'], '201112223333@c.us')

    # ------------------------------------------------------------------ phase 3
    def test_11_members_sync_and_management(self):
        group = self._group()
        partner = self.env['res.partner'].create({'name': 'Mem', 'phone': '+20 100 777 0001'})
        members = [{'id': '201007770001@c.us', 'role': 'admin'}, {'id': {'_serialized': '555@lid'}, 'role': 'participant'}]
        with patch(f'{CLIENT}.get', return_value=members):
            group.with_user(self.manager).action_sync_members()
        self.assertEqual(sorted(group.member_ids.mapped('role')), ['admin', 'participant'])
        member = group.member_ids.filtered(lambda m: m.role == 'admin')
        self.assertEqual(member.partner_id, partner)  # matched by phone, never by name
        self.assertEqual(group.participants_count, 2)
        with patch(f'{CLIENT}.post', return_value={}) as post, patch(f'{CLIENT}.get', return_value=[]):
            group.with_user(self.manager).add_participants(partner)
        self.assertTrue(post.call_args.args[0].endswith('/participants/add'))
        self.assertEqual(post.call_args.args[1], {'participants': [{'id': '201007770001@c.us'}]})
        self.assertFalse(group.member_ids)  # re-synced from the (mocked) empty server list
        with self.assertRaises(AccessError):
            group.with_user(self.user).action_sync_members()

    def test_12_invite_leave(self):
        group = self._group()
        with patch(f'{CLIENT}.get', return_value={'inviteCode': 'ABC123'}):
            group.with_user(self.manager).action_get_invite_link()
        self.assertEqual(group.sudo().invite_link, 'https://chat.whatsapp.com/ABC123')
        with patch(f'{CLIENT}.post', return_value={}) as post:
            group.with_user(self.manager).action_leave()
        self.assertTrue(post.call_args.args[0].endswith('/leave'))
        self.assertFalse(group.allow_send)

    def test_13_incoming_group_messages_opt_in(self):
        import json
        group = self._group()
        event_model = self.env['elblasy.waha.webhook.event']

        def feed(mid, event='message.any', payload=None):
            data = {'id': f'e{mid}', 'event': event, 'session': 'main', 'payload': payload}
            ev = event_model._store_event(self.session, json.dumps(data).encode(), data)
            ev._process_event()
            return ev
        msg = {'id': 'false_1203@g.us_X1', 'timestamp': 1700000000, 'from': '1203@g.us', 'fromMe': False,
               'participant': '2010@c.us', 'body': 'hello group', '_data': {'notifyName': 'Sara'}}
        self.session.inbox_groups = False  # this test is about the separate opt-in group log
        self.assertEqual(feed('1', payload=msg).state, 'ignored')  # privacy: off by default
        group.log_messages = True
        self.assertEqual(feed('2', payload=msg).state, 'done')
        feed('3', payload=msg)  # redelivery
        self.assertEqual(len(group.message_ids), 1)
        self.assertEqual((group.message_ids.sender_name, group.message_ids.body), ('Sara', 'hello group'))
        feed('4', event='group.v2.update', payload={'id': '1203@g.us', 'subject': 'Renamed'})
        self.assertEqual(group.name, 'Renamed')

    def test_14_create_group_wizard(self):
        partner = self.env['res.partner'].create({'name': 'M1', 'phone': '+20 100 888 0001'})
        Wizard = self.env['elblasy.waha.group.create'].with_user(self.manager)
        with patch(f'{CLIENT}.post', return_value={'gid': {'_serialized': '777@g.us'}}) as post:
            res = Wizard.create({'session_id': self.session.id, 'name': 'New team',
                                 'partner_ids': [(6, 0, partner.ids)]}).action_create()
        self.assertEqual(post.call_args.args[1]['participants'], [{'id': '201008880001@c.us'}])
        group = self.env['elblasy.waha.group'].browse(res['res_id'])
        self.assertEqual((group.chat_id, group.allow_send), ('777@g.us', True))
        self.assertIn(group, partner.waha_group_ids)
        with patch(f'{CLIENT}.post', return_value={}):
            with self.assertRaises(UserError):  # WAHA gave no id: the user is told to sync
                Wizard.create({'session_id': self.session.id, 'name': 'Ghost'}).action_create()

    # ------------------------------------------------------------------ phase 5
    def _incoming(self, mid, body, chat='201001230000@c.us', ts=None):
        import json
        import time
        data = {'id': f'e_{mid}', 'event': 'message.any', 'session': 'main', 'payload': {
            'id': mid, 'timestamp': ts or int(time.time()), 'from': chat, 'fromMe': False, 'body': body,
            'hasMedia': False, '_data': {'notifyName': 'Omar'}}}
        ev = self.env['elblasy.waha.webhook.event']._store_event(self.session, json.dumps(data).encode(), data)
        ev._process_event()
        return ev

    def test_15_autoreply_first_message_and_cooldown(self):
        self.env['elblasy.waha.autoreply'].create({
            'name': 'Welcome', 'trigger': 'first', 'reply_text': 'Hi {name}, welcome!', 'cooldown_minutes': 60})
        with patch(f'{CLIENT}.send_text', return_value={'id': 'true_ar1'}) as send:
            self._incoming('false_a1', 'hello')
            self._incoming('false_a2', 'anyone?')  # not the first message any more
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[2], 'Hi Omar, welcome!')
        message = self.env['elblasy.waha.message'].search([('is_auto', '=', True), ('session_id', '=', self.session.id)])
        self.assertEqual((len(message), message.direction, message.user_id.id), (1, 'out', False))

    def test_16_autoreply_keyword_hours_and_old_messages(self):
        Rule = self.env['elblasy.waha.autoreply']
        Rule.create({'name': 'Price', 'trigger': 'keyword', 'keywords': 'price, سعر', 'reply_text': 'See our catalog',
                     'cooldown_minutes': 0, 'sequence': 1})
        Rule.create({'name': 'Closed', 'trigger': 'outside_hours', 'work_from': 0.0, 'work_to': 0.0,
                     'reply_text': 'We are closed', 'cooldown_minutes': 0, 'sequence': 2})
        with patch(f'{CLIENT}.send_text', return_value={'id': 'true_ar2'}) as send:
            self._incoming('false_k1', 'what is the PRICE?', chat='201001230001@c.us')
            self._incoming('false_k2', 'ok thanks', chat='201001230001@c.us')
            self._incoming('false_k3', 'old', chat='201001230001@c.us', ts=1600000000)  # history: ignored
        self.assertEqual([c.args[2] for c in send.call_args_list], ['See our catalog', 'We are closed'])

    def test_17_optout_keyword_blocks_automatic_messages(self):
        with patch(f'{CLIENT}.send_text', return_value={'id': 'true_oo'}) as send:
            self._incoming('false_o1', 'STOP', chat='201001230002@c.us')
        self.assertEqual(send.call_count, 1)  # confirmation
        self.assertTrue(self.env['elblasy.waha.optout']._is_opted_out(self.env.company, '+20 100 123 0002'))
        Post = self.env['elblasy.waha.group.post'].with_user(self.user)
        with patch(f'{CLIENT}.post', return_value={'id': 'x'}) as post:
            with self.assertRaises(UserError):
                Post.send_to_number('+20 100 123 0002', self.session, 'promo')
            Post.send_to_number('+20 100 123 0003', self.session, 'promo')  # other numbers are unaffected
        self.assertEqual(post.call_count, 1)
        self._incoming('false_o2', 'start', chat='201001230002@c.us')
        self.assertFalse(self.env['elblasy.waha.optout']._is_opted_out(self.env.company, '201001230002'))

    def test_18_response_time_report_fields(self):
        import time
        self._incoming('false_r1', 'need help', chat='201001230004@c.us', ts=int(time.time()) - 120)
        conv = self.env['elblasy.waha.conversation'].search([('remote_phone', '=', '201001230004')])
        self.assertTrue(conv.awaiting_since)
        with patch(f'{CLIENT}.send_text', return_value={'id': 'true_rr'}):
            conv.with_user(self.user).inbox_send_text('on it', 'tok-r')
        reply = self.env['elblasy.waha.message'].search([('conversation_id', '=', conv.id), ('direction', '=', 'out')])
        self.assertGreaterEqual(reply.response_seconds, 100)
        self.assertEqual(reply.user_id, self.user)
        self.assertFalse(conv.awaiting_since)
        data = dict(self.env['elblasy.waha.message']._read_group(
            [('session_id', '=', self.session.id), ('direction', '=', 'out')], ['direction'], ['response_seconds:avg']))
        self.assertGreaterEqual(data['out'], 100)  # what the pivot "response time" measure shows

    # ------------------------------------------------------------------ inbox media + LID
    def test_19_inbox_media_is_stored_and_shown(self):
        import json
        payload = {'id': 'false_201001239999@c.us_MEDIA1', 'timestamp': 1700000000, 'from': '201001239999@c.us',
                   'fromMe': False, 'body': 'my photo', 'hasMedia': True,
                   'media': {'url': 'http://internal:3000/api/files/p.jpg', 'mimetype': 'image/jpeg', 'filename': 'p.jpg'}}
        data = {'id': 'e_media', 'event': 'message.any', 'session': 'main', 'payload': payload}
        with patch(f'{CLIENT}.download', return_value=b'JPEGDATA-0123456789') as dl:
            ev = self.env['elblasy.waha.webhook.event']._store_event(self.session, json.dumps(data).encode(), data)
            ev._process_event()
        self.assertEqual(ev.state, 'done')
        self.assertEqual(dl.call_args.args[0], 'http://internal:3000/api/files/p.jpg')
        message = self.env['elblasy.waha.message'].search([('remote_message_id', '=', payload['id'])])
        self.assertEqual((message.message_type, len(message.attachment_ids)), ('image', 1))
        self.assertEqual(bytes(message.attachment_ids.raw), b'JPEGDATA-0123456789')
        ui = message._to_ui()
        self.assertEqual(ui['attachments'], [{'id': message.attachment_ids.id, 'name': 'p.jpg', 'image': True,
                                              'mimetype': 'image/jpeg', 'size': message.attachment_ids.file_size}])
        # users of the inbox can open it, others cannot
        self.assertTrue(message.with_user(self.user).attachment_ids.read(['name']))

    def test_20_inbox_media_download_failure_keeps_message(self):
        import json
        payload = {'id': 'false_201001238888@c.us_MEDIA2', 'timestamp': 1700000001, 'from': '201001238888@c.us',
                   'fromMe': False, 'hasMedia': True,
                   'media': {'url': 'http://internal:3000/api/files/x.pdf', 'mimetype': 'application/pdf'}}
        data = {'id': 'e_media2', 'event': 'message.any', 'session': 'main', 'payload': payload}
        with patch(f'{CLIENT}.download', side_effect=WahaError('network', 'down')):
            ev = self.env['elblasy.waha.webhook.event']._store_event(self.session, json.dumps(data).encode(), data)
            ev._process_event()
        self.assertEqual(ev.state, 'done')
        message = self.env['elblasy.waha.message'].search([('remote_message_id', '=', payload['id'])])
        self.assertEqual((message.message_type, len(message.attachment_ids)), ('document', 0))
        self.assertIn('Media not downloaded', message.failure_reason)

    def test_21_lid_chat_gets_phone_and_contact(self):
        partner = self.env['res.partner'].create({'name': 'Lid Owner', 'phone': '+20 100 456 7000'})
        with patch(f'{CLIENT}.lid_to_phone', return_value='201004567000'):
            conv = self.env['elblasy.waha.conversation']._get_or_create(self.session, '98765@lid', 'Someone')
        self.assertEqual((conv.chat_type, conv.remote_phone, conv.partner_id), ('lid', '201004567000', partner))
        with patch(f'{CLIENT}.lid_to_phone', side_effect=WahaError('network', 'x')):
            other = self.env['elblasy.waha.conversation']._get_or_create(self.session, '11111@lid')
        self.assertFalse(other.remote_phone)  # still usable through the LID
        with patch(f'{CLIENT}.lid_to_phone', return_value='201004567001'):
            other.action_resolve_lid()
        self.assertEqual(other.remote_phone, '201004567001')

    # ------------------------------------------------------------------ polling fallback (no webhook)
    def test_22_auto_sync_messages_without_webhook(self):
        import time
        now_ts = int(time.time())
        chat = {'id': '201001235555@c.us', 'lastMessage': {'timestamp': now_ts}}
        old_chat = {'id': '201001236666@c.us', 'lastMessage': {'timestamp': 1600000000}}
        msg = {'id': 'false_201001235555@c.us_POLL1', 'timestamp': now_ts + 5, 'from': '201001235555@c.us',
               'fromMe': False, 'body': 'hello by polling', 'hasMedia': False}
        calls = []

        def fake_get(self_, path, params=None):
            calls.append(path)
            return [chat, old_chat] if path.endswith('/overview') else [dict(msg)]
        self.session.auto_sync_messages = True
        with patch(f'{CLIENT}.get', fake_get):
            self.env['elblasy.waha.session']._cron_sync_messages()  # first run only sets the starting point
            self.assertFalse(self.env['elblasy.waha.message'].search([('session_id', '=', self.session.id)]))
            self.assertTrue(self.session.messages_synced_at)
            self.session.messages_synced_at = fields_dt_sub_minutes(1)
            self.env['elblasy.waha.session']._cron_sync_messages()
            self.session.messages_synced_at = fields_dt_sub_minutes(1)
            self.env['elblasy.waha.session']._cron_sync_messages()  # again: no duplicate
        stored = self.env['elblasy.waha.message'].search([('session_id', '=', self.session.id)])
        self.assertEqual((len(stored), stored.body, stored.direction), (1, 'hello by polling', 'in'))
        self.assertFalse([c for c in calls if '201001236666' in c])  # chat without news: no extra API call
        self.session.auto_sync_messages = False
        with patch(f'{CLIENT}.get') as get:
            self.env['elblasy.waha.session']._cron_sync_messages()
        get.assert_not_called()

    # ------------------------------------------------------------------ number verification
    def test_23_number_check(self):
        Post = self.env['elblasy.waha.group.post'].with_user(self.user)
        with patch(f'{CLIENT}.post', return_value={'id': 'x'}) as post:
            self.check_exists.return_value = (False, None)
            with self.assertRaises(UserError):
                Post.send_to_number('+20 100 999 0001', self.session, 'hi')
            post.assert_not_called()  # nothing is sent to an unregistered number
            self.check_exists.return_value = (True, '555123@lid')
            Post.send_to_number('+20 100 999 0002', self.session, 'hi')
            self.assertEqual(post.call_args.args[1]['chatId'], '555123@lid')  # WAHA's own chat id wins
            self.check_exists.side_effect = WahaError('not_found', 'no endpoint', 404)
            Post.send_to_number('+20 100 999 0003', self.session, 'hi')
            self.assertEqual(post.call_args.args[1]['chatId'], '201009990003@c.us')  # unsupported: not blocked
            self.check_exists.side_effect = WahaError('auth', 'bad key', 401)
            with self.assertRaises(UserError):
                Post.send_to_number('+20 100 999 0004', self.session, 'hi')
            self.check_exists.side_effect = None
            self.session.check_numbers = False
            self.check_exists.reset_mock()
            Post.send_to_number('+20 100 999 0005', self.session, 'hi')
            self.check_exists.assert_not_called()

    # ------------------------------------------------------------------ files from the inbox
    def test_24_inbox_send_file(self):
        import base64
        import json
        self.session.status = 'WORKING'
        data = {'id': 'e_file_in', 'event': 'message.any', 'session': 'main', 'payload': {
            'id': 'false_201001237777@c.us_F0', 'timestamp': 1700000000, 'from': '201001237777@c.us',
            'fromMe': False, 'body': 'hi', 'hasMedia': False}}
        self.env['elblasy.waha.webhook.event']._store_event(self.session, json.dumps(data).encode(), data)._process_event()
        conv = self.env['elblasy.waha.conversation'].search([('remote_phone', '=', '201001237777')])
        b64 = base64.b64encode(b'%PDF-1.4 test').decode()
        with patch(f'{CLIENT}.post', return_value={'id': 'true_file1'}) as post:
            ui = conv.with_user(self.user).inbox_send_file('invoice.pdf', 'application/pdf', b64, 'Your invoice', 'tok-f')
            again = conv.with_user(self.user).inbox_send_file('invoice.pdf', 'application/pdf', b64, 'Your invoice', 'tok-f')
        self.assertEqual(post.call_count, 1)  # double click safe
        path, payload = post.call_args.args
        self.assertEqual((path, payload['caption'], payload['file']['filename']), ('/api/sendFile', 'Your invoice', 'invoice.pdf'))
        self.assertEqual((ui['type'], ui['status'], ui['attachments'][0]['name']), ('document', 'sent', 'invoice.pdf'))
        self.assertEqual(ui['id'], again['id'])
        with patch(f'{CLIENT}.post', return_value={'id': 'true_img'}) as post:
            conv.with_user(self.user).inbox_send_file('p.png', 'image/png', b64)
        self.assertEqual(post.call_args.args[0], '/api/sendImage')
        for bad in ('!!notbase64!!', ''):
            with self.assertRaises(UserError):
                conv.with_user(self.user).inbox_send_file('x.bin', 'application/octet-stream', bad)
        with patch.object(type(conv), 'MAX_FILE_BYTES', 5), self.assertRaises(UserError):
            conv.with_user(self.user).inbox_send_file('big.bin', 'application/octet-stream', b64)

    # ------------------------------------------------------------------ phone-sent messages via polling
    def test_25_sync_imports_messages_sent_from_phone(self):
        import time
        now_ts = int(time.time())
        msg = {'id': 'true_201001234444@c.us_PH1', 'timestamp': now_ts + 3, 'from': '999@c.us',
               'to': '201001234444@c.us', 'fromMe': True, 'body': 'typed on my phone', 'hasMedia': False, 'ack': 2}

        def fake_get(self_, path, params=None):
            return [{'id': '201001234444@c.us', 'lastMessage': {'timestamp': now_ts}}] \
                if path.endswith('/overview') else [dict(msg)]
        with patch(f'{CLIENT}.get', fake_get):
            self.session.messages_synced_at = fields_dt_sub_minutes(1)
            self.session.sudo()._sync_messages()
        stored = self.env['elblasy.waha.message'].search([('remote_message_id', '=', msg['id'])])
        self.assertEqual((stored.direction, stored.body, stored.delivery_status), ('out', 'typed on my phone', 'delivered'))

    # ------------------------------------------------------------------ privacy & multi-company
    def test_26_users_see_only_their_posts_and_own_company(self):
        group = self._group()
        other_user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'U2', 'login': 'gu2', 'email': 'gu2@example.com',
            'group_ids': [(6, 0, [self.env.ref('elblasy_waha_connector.group_waha_user').id])]})
        with patch(f'{CLIENT}.post', return_value={'id': 'x'}):
            mine = self.env['elblasy.waha.group.post'].with_user(self.user).send_to_group(group, 'private to me')
        self.assertIn(mine, self.env['elblasy.waha.group.post'].with_user(self.user).search([]))
        self.assertNotIn(mine, self.env['elblasy.waha.group.post'].with_user(other_user).search([]))
        self.assertIn(mine, self.env['elblasy.waha.group.post'].with_user(self.manager).search([]))
        company2 = self.env['res.company'].create({'name': 'Other Co'})
        server2 = self.env['elblasy.waha.server'].create({'name': 'S2', 'base_url': 'http://o.test', 'company_id': company2.id})
        session2 = self.env['elblasy.waha.session'].create({'name': 'S2', 'session_name': 's2', 'server_id': server2.id})
        foreign = self.env['elblasy.waha.group'].create({'name': 'Foreign', 'session_id': session2.id, 'chat_id': '9@g.us'})
        self.assertNotIn(foreign, self.env['elblasy.waha.group'].with_user(self.user).search([]))
        self.assertIn(group, self.env['elblasy.waha.group'].with_user(self.user).search([]))

    # ------------------------------------------------------------------ WAHA server capabilities (study)
    def test_27_group_metadata_from_waha_dashboard_shape(self):
        self._sync([{'id': {'_serialized': '1203@g.us', 'server': 'g.us'}, 'name': 'Team',
                     'groupMetadata': {'subject': 'Team (official)', 'desc': 'Rules', 'size': 12,
                                       'announce': True, 'restrict': False}}])
        group = self.env['elblasy.waha.group'].search([('chat_id', '=', '1203@g.us')])
        self.assertEqual((group.name, group.description, group.participants_count),
                         ('Team (official)', 'Rules', 12))
        self.assertEqual((group.messages_admin_only, group.info_admin_only), (True, False))

    def test_28_group_and_chat_pictures(self):
        import base64
        group = self._group()
        conv = self.env['elblasy.waha.conversation']._get_or_create(self.session, '201001235000@c.us', 'Pic') \
            if False else None
        import base64 as _b64
        png = _b64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')
        with patch(f'{CLIENT}.picture_url', return_value='https://pps.whatsapp.net/g.jpg') as url, \
                patch(f'{CLIENT}.download_picture', return_value=png):
            self.env['elblasy.waha.group']._cron_sync_pictures()
        self.assertTrue(group.image_128)
        self.assertTrue(url.call_args.kwargs.get('group'))
        # no picture (privacy) is remembered so the server is not asked again every 15 minutes
        other = self.env['elblasy.waha.group'].create({'name': 'NoPic', 'session_id': self.session.id, 'chat_id': '55@g.us'})
        with patch(f'{CLIENT}.picture_url', return_value=False) as url:
            self.env['elblasy.waha.group']._cron_sync_pictures()
        self.assertFalse(other.image_128)
        self.assertTrue(other.picture_synced_at)
        with patch(f'{CLIENT}.picture_url') as url:
            self.env['elblasy.waha.group']._cron_sync_pictures()
        url.assert_not_called()
        # chats
        conv = self.env['elblasy.waha.conversation'].create({
            'session_id': self.session.id, 'remote_chat_id': '201001235000@c.us'})
        with patch(f'{CLIENT}.picture_url', return_value='https://pps.whatsapp.net/c.jpg'), \
                patch(f'{CLIENT}.download_picture', return_value=png), \
                patch(f'{CLIENT}.contact_name', return_value='Saved Name') as contact_name:
            self.env['elblasy.waha.conversation']._cron_sync_pictures()
        self.assertTrue(conv.image_128)
        self.assertEqual(conv.name, 'Saved Name')  # WhatsApp name instead of the bare number
        self.assertEqual(contact_name.call_count, 1)
        row = self.env['elblasy.waha.conversation'].with_user(self.user).inbox_conversations(session_id=self.session.id)
        self.assertTrue(next(r for r in row if r['id'] == conv.id)['has_image'])
        # an unreachable server stops the batch instead of hammering it
        conv2 = self.env['elblasy.waha.conversation'].create({
            'session_id': self.session.id, 'remote_chat_id': '201001235001@c.us'})
        with patch(f'{CLIENT}.picture_url', side_effect=WahaError('network', 'down')) as url, \
                patch(f'{CLIENT}.contact_name', return_value=False):
            self.env['elblasy.waha.conversation']._cron_sync_pictures()
        self.assertEqual(url.call_count, 1)
        self.assertFalse(conv2.picture_synced_at)

    def test_29_picture_download_only_from_whatsapp_hosts(self):
        from odoo.addons.elblasy_waha_connector.models.waha_client import WahaClient
        client = WahaClient('http://waha.test', 'K')
        for bad in ('http://pps.whatsapp.net/x.jpg', 'https://evil.example.com/x.jpg',
                    'https://pps.whatsapp.net.evil.com/x.jpg', 'file:///etc/passwd'):
            with self.assertRaises(WahaError):
                client.download_picture(bad)

    def test_30_messaging_limits_guard_new_contacts_only(self):
        self.session._apply_remote({'status': 'WORKING', 'me': {
            'id': '2010@c.us', 'messageCapping': {'cappingStatus': 'CAPPED', 'totalQuota': 50, 'usedQuota': 50},
            'reachoutTimelock': {'isActive': True, 'timeEnforcementEnds': 4102444800000}}})
        self.assertEqual((self.session.capping_used, self.session.capping_total, self.session.timelock_active),
                         (50, 50, True))
        self.assertEqual(self.session.timelock_ends.year, 2100)
        Post = self.env['elblasy.waha.group.post'].with_user(self.user)
        with patch(f'{CLIENT}.post', return_value={'id': 'x'}) as post:
            with self.assertRaises(UserError):  # never chatted with this number
                Post.send_to_number('+20 100 777 1111', self.session, 'cold message')
            self.env['elblasy.waha.conversation'].create({
                'session_id': self.session.id, 'remote_chat_id': '201007771111@c.us'})
            Post.send_to_number('+20 100 777 1111', self.session, 'existing chat is fine')  # not restricted
        self.assertEqual(post.call_count, 1)
        self.session._apply_remote({'status': 'WORKING', 'limits': {'reachoutTimelock': None}})
        self.assertFalse(self.session.timelock_active)  # status events clear the lock

    # ------------------------------------------------------------------ batch 2: labels, calls, read state
    def _event(self, etype, payload, key):
        import json
        data = {'id': key, 'event': etype, 'session': 'main', 'payload': payload}
        ev = self.env['elblasy.waha.webhook.event']._store_event(self.session, json.dumps(data).encode(), data)
        ev._process_event()
        return ev

    def test_31_labels_sync_events_and_toggle(self):
        Conv = self.env['elblasy.waha.conversation']
        vip = Conv.create({'session_id': self.session.id, 'remote_chat_id': '201001236001@c.us'})
        plain = Conv.create({'session_id': self.session.id, 'remote_chat_id': '201001236002@c.us'})

        def fake_get(self_, path, params=None):
            if path.endswith('/labels'):
                return [{'id': '10', 'name': 'Favourites', 'colorHex': '#ff9485'},
                        {'id': '12', 'name': 'VIP', 'colorHex': '#83e422'}]
            return [{'id': {'_serialized': '201001236001@c.us'}}] if path.endswith('/12/chats') else []
        with patch(f'{CLIENT}.get', fake_get):
            self.session.with_user(self.manager).action_sync_labels()
        labels = self.env['elblasy.waha.label'].search([('session_id', '=', self.session.id)])
        self.assertEqual(sorted(labels.mapped('name')), ['Favourites', 'VIP'])
        self.assertEqual(vip.label_ids.name, 'VIP')
        self.assertFalse(plain.label_ids)
        # server pushed events keep Odoo in sync
        fav = labels.filtered(lambda l: l.label_id == '10')
        self.assertEqual(self._event('label.chat.added', {'labelId': '10', 'chatId': plain.remote_chat_id}, 'l1').state, 'done')
        self.assertEqual(plain.label_ids, fav)
        self._event('label.chat.deleted', {'labelId': '10', 'chatId': plain.remote_chat_id}, 'l2')
        self.assertFalse(plain.label_ids)
        self._event('label.upsert', {'id': '10', 'name': 'Fav renamed', 'colorHex': '#000000'}, 'l3')
        self.assertEqual(fav.name, 'Fav renamed')
        # inbox: labels are listed, filterable and togglable (written to the phone first)
        rows = Conv.with_user(self.user).inbox_conversations(session_id=self.session.id, label_id=labels.filtered(lambda l: l.label_id == '12').id)
        self.assertEqual([r['id'] for r in rows], [vip.id])
        self.assertEqual(rows[0]['labels'][0]['name'], 'VIP')
        with patch(f'{CLIENT}.put', return_value={}) as put:
            result = plain.with_user(self.user).inbox_toggle_label(fav.id)
        self.assertEqual(put.call_args.args[1], {'labels': [{'id': '10'}]})
        self.assertEqual([l['name'] for l in result], ['Fav renamed'])
        with patch(f'{CLIENT}.put', side_effect=WahaError('http', 'no labels', 500)):
            with self.assertRaises(UserError):  # personal (non Business) accounts: the phone refuses
                plain.with_user(self.user).inbox_toggle_label(fav.id)
        self.assertEqual(plain.label_ids, fav)  # unchanged locally
        self._event('label.deleted', {'id': '10'}, 'l4')
        self.assertFalse(self.env['elblasy.waha.label'].search([('label_id', '=', '10'), ('session_id', '=', self.session.id)]))
        other_session_label = self.env['elblasy.waha.label']._upsert(self.session, {'id': '99', 'name': 'X'})
        self.assertTrue(other_session_label)

    def test_32_reject_calls(self):
        payload = {'id': 'call_1', 'from': '201001237000@c.us', 'isVideo': False}
        self.assertEqual(self._event('call.received', payload, 'c0').state, 'ignored')  # feature is opt-in
        self.session.reject_calls = True
        self.assertIn('call.received', self.session._webhook_events())
        with patch(f'{CLIENT}.post', return_value={}) as post, patch(f'{CLIENT}.send_text', return_value={}) as send:
            self.assertEqual(self._event('call.received', payload, 'c1').state, 'done')
            self._event('call.received', dict(payload, id='call_2'), 'c2')  # same caller again: rejected, not re-messaged
        self.assertEqual([c.args[0] for c in post.call_args_list], ['/api/main/calls/reject'] * 2)
        self.assertEqual(post.call_args_list[0].args[1], {'from': '201001237000@c.us', 'id': 'call_1'})
        self.assertEqual(send.call_count, 1)
        calls = self.env['elblasy.waha.call'].search([('session_id', '=', self.session.id)])
        self.assertEqual((len(calls), set(calls.mapped('rejected'))), (2, {True}))
        self.assertEqual(sorted(calls.mapped('replied')), [False, True])

    def test_33_read_state_is_mirrored_on_the_phone(self):
        self.session.status = 'WORKING'
        conv = self.env['elblasy.waha.conversation'].create({
            'session_id': self.session.id, 'remote_chat_id': '201001238500@c.us', 'unread_count': 2})
        with patch(f'{CLIENT}.post', return_value={}) as post:
            conv.with_user(self.user).inbox_mark_read()
            conv.with_user(self.user).inbox_mark_read()  # nothing unread any more: no second call
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], '/api/main/chats/201001238500@c.us/messages/read')
        conv.unread_count = 1
        with patch(f'{CLIENT}.post', side_effect=WahaError('network', 'down')):
            conv.with_user(self.user).inbox_mark_read()  # best effort: never blocks reading in Odoo
        self.assertEqual(conv.unread_count, 0)
        conv.unread_count = 1
        self.session.sync_read_state = False
        with patch(f'{CLIENT}.post') as post:
            conv.with_user(self.user).inbox_mark_read()
        post.assert_not_called()

    def test_34_employee_button_comes_with_this_module(self):
        if 'hr.employee' not in self.env:
            self.skipTest("hr is not installed")
        view = self.env.ref('elblasy_waha_connector.hr_employee_view_form_waha')
        self.assertEqual(view.model, 'hr.employee')
        self.assertIn('elblasy_waha_connector.waha_group_send_action', view.arch)
        # the window opened by the button needs no template and prefills the employee's own numbers
        employee = self.env['hr.employee'].create({'name': 'Emp Btn', 'mobile_phone': '+20 111 000 2222'})
        wiz = self.env['elblasy.waha.group.send'].with_user(self.user).with_context(
            active_model='hr.employee', active_id=employee.id).create({'session_id': self.session.id})
        self.assertEqual((wiz.phone, wiz.send_to_customer), ('+20 111 000 2222', True))

    # ------------------------------------------------------------------ slow WAHA (production)
    def _sync_fixtures(self):
        import time
        now = int(time.time())
        chats = [{'id': f'20100123{i}000@c.us', 'lastMessage': {'timestamp': now}} for i in range(1, 4)]
        msgs = {c['id']: [{'id': f"false_{c['id']}_S", 'timestamp': now + 2, 'from': c['id'], 'fromMe': False,
                           'body': f"hello from {c['id'][:9]}", 'hasMedia': False}] for c in chats}
        return chats, msgs

    def test_35_sync_skips_a_slow_chat_keeps_the_rest_and_does_not_advance(self):
        chats, msgs = self._sync_fixtures()
        seen = []

        def fake_get(self_, path, params=None):
            if path.endswith('/overview'):
                return chats
            chat_id = path.split('/chats/')[1].split('/messages')[0]
            seen.append((chat_id, params.get('filter.timestamp.gte')))
            if chat_id == chats[1]['id']:
                raise WahaError('network', 'The WAHA server did not answer in time.')
            return msgs[chat_id]
        self.session.messages_synced_at = fields_dt_sub_minutes(5)
        before = self.session.messages_synced_at
        with patch(f'{CLIENT}.get', fake_get):
            res = self.session.with_user(self.manager).action_sync_messages()
        self.assertEqual(res['params']['type'], 'warning')  # partial result, told to the user
        self.assertEqual(self.env['elblasy.waha.message'].search_count([('session_id', '=', self.session.id)]), 2)
        self.assertEqual(self.session.messages_synced_at, before)  # nothing can be lost: next run retries
        self.assertTrue(all(ts for _chat, ts in seen))  # only messages newer than the last sync are requested
        with patch(f'{CLIENT}.get', lambda s_, p, params=None: chats if p.endswith('/overview') else msgs[p.split('/chats/')[1].split('/messages')[0]]):
            res = self.session.with_user(self.manager).action_sync_messages()
        self.assertEqual(res['params']['type'], 'success')
        self.assertEqual(self.env['elblasy.waha.message'].search_count([('session_id', '=', self.session.id)]), 3)
        self.assertGreater(self.session.messages_synced_at, before)

    def test_36_sync_stops_when_server_is_down_or_out_of_time(self):
        chats, msgs = self._sync_fixtures()
        calls = []

        def down(self_, path, params=None):
            if path.endswith('/overview'):
                return chats * 3  # many chats
            calls.append(path)
            raise WahaError('network', 'down')
        self.session.messages_synced_at = fields_dt_sub_minutes(5)
        with patch(f'{CLIENT}.get', down):
            imported, skipped = self.session._sync_messages()
        self.assertEqual(len(calls), 3)  # three failures in a row: stop instead of hammering WAHA
        self.assertEqual(imported, 0)
        with patch(f'{CLIENT}.get', lambda s_, p, params=None: chats if p.endswith('/overview') else msgs[p.split('/chats/')[1].split('/messages')[0]]):
            imported, skipped = self.session._sync_messages(budget=-1)  # no time left
        self.assertEqual((imported, skipped), (0, 3))
        # the overview itself failing is a real error for the user
        with patch(f'{CLIENT}.get', side_effect=WahaError('network', 'down')):
            with self.assertRaises(UserError):
                self.session.with_user(self.manager).action_sync_messages()

    def test_37_cron_refreshes_status_of_a_failing_session(self):
        self.session.auto_sync_messages = True
        self.session.messages_synced_at = fields_dt_sub_minutes(2)
        with patch(f'{CLIENT}.get', side_effect=WahaError('http', 'Session status is not as expected', 422)), \
                patch(f'{CLIENT}.get_session', return_value={'status': 'FAILED'}) as status:
            self.env['elblasy.waha.session']._cron_sync_messages()
        status.assert_called_once()
        self.assertEqual(self.session.status, 'FAILED')  # so the next cron run skips it
        with patch(f'{CLIENT}.get') as get:
            self.env['elblasy.waha.session']._cron_sync_messages()
        get.assert_not_called()

    # ------------------------------------------------------------------ review fixes
    def test_38_users_read_only_on_conversations_and_messages(self):
        from odoo.exceptions import AccessError as AE
        conv = self.env['elblasy.waha.conversation'].create({
            'session_id': self.session.id, 'remote_chat_id': '201001239100@c.us'})
        with self.assertRaises(AE):  # no fake "incoming" messages through RPC
            self.env['elblasy.waha.message'].with_user(self.user).create({
                'conversation_id': conv.id, 'direction': 'in', 'body': 'fake'})
        with self.assertRaises(AE):  # cannot redirect a chat to another number
            conv.with_user(self.user).write({'remote_chat_id': '999@c.us'})
        # ...but every inbox action still works for them
        with patch(f'{CLIENT}.send_text', return_value={'id': 'true_ro'}):
            ui = conv.with_user(self.user).inbox_send_text('allowed', 'tok-ro')
        self.assertEqual(ui['status'], 'sent')
        self.user.group_ids = [(4, self.env.ref('base.group_partner_manager').id)]  # creating contacts needs contact rights
        pid = conv.with_user(self.user).action_create_partner()
        self.assertEqual(conv.partner_id.id, pid)

    def test_39_unknown_acks_are_dropped_and_malformed_events_not_retried(self):
        from odoo.addons.elblasy_waha_connector.models.waha_webhook_event import MAX_ATTEMPTS
        ack = self._event('message.ack', {'id': 'true_never_seen', 'ack': 2}, 'ack-x')
        self.assertEqual(ack.state, 'failed')  # may still be overtaking its message
        for _i in range(MAX_ATTEMPTS):
            ack._process_event()
        self.assertEqual(ack.state, 'ignored')  # gave up: not retried forever, purged with the done ones
        bad = self._event('message.any', {'nothing': 'useful'}, 'bad-x')
        self.assertEqual((bad.state, bad.attempts), ('failed', MAX_ATTEMPTS))
        self.assertFalse(self.env['elblasy.waha.webhook.event'].search(
            [('id', '=', bad.id), ('state', 'in', ('failed', 'pending')), ('attempts', '<', MAX_ATTEMPTS)]))
        self.env.cr.execute("UPDATE elblasy_waha_webhook_event SET create_date = now() - interval '31 days' WHERE id = %s",
                            [bad.id])
        bad.invalidate_recordset()
        with patch.object(type(self.env['ir.cron']), '_commit_progress', return_value=60.0):
            self.env['elblasy.waha.webhook.event']._cron_retry_failed()
        self.assertFalse(bad.exists())  # failed events do not pile up forever

    def test_40_store_event_only_swallows_real_duplicates(self):
        import json
        data = {'id': 'evt-dup', 'event': 'message.any', 'session': 'main', 'payload': {}}
        Event = self.env['elblasy.waha.webhook.event']
        self.assertTrue(Event._store_event(self.session, json.dumps(data).encode(), data))
        self.assertFalse(Event._store_event(self.session, json.dumps(data).encode(), data))
        with patch.object(type(Event), 'create', side_effect=RuntimeError('database is broken')):
            with self.assertRaises(RuntimeError):
                Event._store_event(self.session, b'{}', dict(data, id='evt-other'))

    def test_41_waha_error_message_is_shown_and_last_error_kept(self):
        from odoo.addons.elblasy_waha_connector.models.waha_client import WahaClient

        class Resp:
            status_code = 422
            content = b'x'

            def json(self):
                return {'error': 'Session status is not as expected', 'status': 'FAILED'}
        with patch('requests.Session.request', return_value=Resp()):
            with self.assertRaises(WahaError) as cm:
                WahaClient('http://waha.test', 'K').get_session('main')
        self.assertIn('Session status is not as expected', str(cm.exception))
        # stored through its own short transaction so the rollback of the failed action does not erase it
        # (checked against a real database: the separate cursor cannot see a test's uncommitted records)
        Session = type(self.env['elblasy.waha.session'])
        with patch(f'{CLIENT}.get_session', side_effect=WahaError('http', 'HTTP 422: Session status is not as expected', 422)), \
                patch.object(Session, '_store_last_error', autospec=True) as store:
            with self.assertRaises(UserError):
                self.session.with_user(self.manager).action_refresh_status()
        self.assertIn('Session status', store.call_args.args[1])

    # ------------------------------------------------------------------ background webhook processing
    def test_42_cron_processes_events_in_arrival_order(self):
        import json
        Event = self.env['elblasy.waha.webhook.event']
        msg = {'id': 'true_201001234567@c.us_ORD', 'from': '201001234567@c.us', 'fromMe': False,
               'body': 'first', 'timestamp': 1700000000}
        events = Event
        for key, etype, payload in (('o1', 'message.any', msg),
                                    ('o2', 'message.ack', {'id': msg['id'], 'ack': 3})):
            data = {'id': key, 'event': etype, 'session': self.session.session_name, 'payload': payload}
            events |= Event._store_event(self.session, json.dumps(data).encode(), data)
        self.assertEqual(set(events.mapped('state')), {'pending'})  # stored only; nothing done in the request
        with patch.object(type(self.env['ir.cron']), '_commit_progress', return_value=60.0) as progress:
            Event._cron_retry_failed()
        self.assertEqual(events.mapped('state'), ['done', 'done'])
        message = self.env['elblasy.waha.message'].search([('remote_message_id', '=', msg['id'])])
        self.assertEqual(message.delivery_status, 'read')  # the ack found its message: same batch, id order
        self.assertEqual(progress.call_count, 3)  # one commit for the count, one after each event

    def test_43_disabled_cron_falls_back_to_inline_processing(self):
        import json
        self.env.ref('elblasy_waha_connector.cron_waha_retry_events').active = False
        data = {'id': 'inline-1', 'event': 'session.status', 'session': self.session.session_name,
                'payload': {'status': 'WORKING'}}
        event = self.env['elblasy.waha.webhook.event']._store_event(self.session, json.dumps(data).encode(), data)
        event._schedule_processing()
        self.assertEqual(event.state, 'done')

    # ------------------------------------------------------------------ phone numbers
    def test_44_national_numbers_get_the_country_code(self):
        from odoo.addons.elblasy_waha_connector.models.res_partner import wa_digits
        egypt, italy = self.env.ref('base.eg'), self.env.ref('base.it')
        self.assertEqual(wa_digits('010 1234 5678', egypt), '201012345678')
        self.assertEqual(wa_digits('+20 10 1234 5678', egypt), '201012345678')
        self.assertEqual(wa_digits('0020-10-1234-5678', egypt), '201012345678')
        self.assertEqual(wa_digits('201012345678', egypt), '201012345678')
        self.assertEqual(wa_digits('010 1234 5678'), '01012345678')  # unknown country: untouched
        self.assertEqual(wa_digits('06 1234 5678', italy), '0612345678')  # Italy keeps its leading 0
        partner = self.env['res.partner'].create({'name': 'Local', 'phone': '01012345678', 'country_id': egypt.id})
        self.assertEqual(partner._waha_phone(), '201012345678')
        with patch(f'{CLIENT}.check_exists', return_value=(True, None)), \
                patch(f'{CLIENT}.post', return_value={'id': 'true_201012345678@c.us_L'}) as post:
            self.env['elblasy.waha.group.post'].send_to_partner(partner, self.session, 'hi')
        self.assertEqual(post.call_args[0][1]['chatId'], '201012345678@c.us')


    # ------------------------------------------------------------------ WhatsApp Web like inbox
    def _msg(self, key, **extra):
        payload = {'id': f'false_201001239000@c.us_{key}', 'from': '201001239000@c.us', 'fromMe': False,
                   'timestamp': 1700000000, **extra}
        self._event('message.any', payload, f'evt-{key}')
        return self.env['elblasy.waha.message'].search([('remote_message_id', '=', payload['id'])])

    def test_45_message_kinds_like_whatsapp(self):
        self.assertFalse(self._msg('e2e', _data={'type': 'e2e_notification'}))  # system notice: no bubble
        loc = self._msg('loc', body='/9j/4AAQSkZJRgABAQ', _data={'type': 'location'},
                        location={'latitude': '30.0444', 'longitude': '31.2357', 'description': 'Cairo'})
        self.assertEqual((loc.message_type, loc.body), ('location', 'Cairo'))  # thumbnail never shown as text
        self.assertAlmostEqual(loc.latitude, 30.0444, places=4)
        card = self._msg('vc', _data={'type': 'vcard'},
                         vCards=['BEGIN:VCARD\nVERSION:3.0\nFN:Ali Hassan\nTEL;type=CELL:+20 100 123 4567\nEND:VCARD'])
        self.assertEqual((card.message_type, card.body), ('contact', 'Ali Hassan\n+20 100 123 4567'))
        poll = self._msg('poll', _data={'type': 'poll_creation', 'pollName': 'Lunch?',
                                        'pollOptions': [{'name': 'Yes'}, {'name': 'No'}]})
        self.assertEqual((poll.message_type, poll.body), ('poll', 'Lunch?\n• Yes\n• No'))
        sticker = self._msg('st', hasMedia=True, _data={'type': 'sticker'})
        self.assertEqual(sticker.message_type, 'sticker')
        voice = self._msg('ptt', hasMedia=True, _data={'type': 'ptt'}, media={'mimetype': 'audio/ogg; codecs=opus'})
        self.assertEqual(voice.message_type, 'audio')
        reply = self._msg('rep', body='yes', replyTo={'id': 'x', 'body': 'Are you coming?'},
                          _data={'type': 'chat', 'isForwarded': True})
        ui = reply._to_ui()
        self.assertEqual((ui['quoted'], ui['forwarded'], ui['type']), ('Are you coming?', True, 'text'))

    def test_46_reactions_edits_and_deletes(self):
        message = self._msg('base', body='Hello')
        self._event('message.reaction', {'id': 'r1', 'from': '201001239000@c.us', 'fromMe': False,
                                         'reaction': {'text': '❤️', 'messageId': message.remote_message_id}}, 'r1')
        self._event('message.reaction', {'id': 'r2', 'fromMe': True,
                                         'reaction': {'text': '❤️', 'messageId': message.remote_message_id}}, 'r2')
        self.assertEqual(message._to_ui()['reactions'], [{'emoji': '❤️', 'count': 2, 'mine': True}])
        self._event('message.reaction', {'id': 'r3', 'fromMe': True,
                                         'reaction': {'text': '', 'messageId': message.remote_message_id}}, 'r3')
        self.assertEqual(message._to_ui()['reactions'], [{'emoji': '❤️', 'count': 1, 'mine': False}])
        self._event('message.edited', {'id': 'e1', 'body': 'Hello there', 'editedMessageId': message.remote_message_id},
                    'e1')
        self.assertEqual((message.body, message.is_edited), ('Hello there', True))
        self._event('message.revoked', {'revokedMessageId': message.remote_message_id, 'before': {}}, 'd1')
        self.assertTrue(message._to_ui()['revoked'])
        self.assertEqual(message.body, 'Hello there')  # kept in Odoo for the record
        unknown = self._event('message.reaction', {'id': 'r4', 'reaction': {'text': '👍', 'messageId': 'nope'}}, 'r4')
        self.assertEqual(unknown.state, 'ignored')

    def test_47_one_chat_per_person_phone_and_hidden_id(self):
        Conv = self.env['elblasy.waha.conversation']
        phone_chat = Conv._get_or_create(self.session, '201001239100@c.us')
        with patch(f'{CLIENT}.lid_to_phone', return_value='201001239100'):
            same = Conv._get_or_create(self.session, '77001@lid')
        self.assertEqual(same, phone_chat)
        self.assertEqual(phone_chat.lid_chat_id, '77001@lid')
        self.assertEqual(Conv._get_or_create(self.session, '77001@lid'), phone_chat)  # no WAHA call needed now
        # a chat first known by its hidden id switches to the phone id once WhatsApp gives it
        with patch(f'{CLIENT}.lid_to_phone', return_value='201001239200'):
            lid_chat = Conv._get_or_create(self.session, '77002@lid')
        self.assertEqual(Conv._get_or_create(self.session, '201001239200@c.us'), lid_chat)
        self.assertEqual((lid_chat.remote_chat_id, lid_chat.lid_chat_id), ('201001239200@c.us', '77002@lid'))

    def test_48_merge_existing_duplicates(self):
        Conv = self.env['elblasy.waha.conversation']
        a = Conv.create({'session_id': self.session.id, 'remote_chat_id': '201001239300@c.us',
                         'remote_phone': '201001239300'})
        b = Conv.create({'session_id': self.session.id, 'remote_chat_id': '88001@lid', 'remote_phone': '201001239300',
                         'display_name_remote': 'Mona'})
        Message = self.env['elblasy.waha.message']
        Message.create({'conversation_id': a.id, 'direction': 'in', 'body': 'old', 'remote_message_id': 'm-a',
                        'date': '2026-10-01 10:00:00'})
        Message.create({'conversation_id': b.id, 'direction': 'in', 'body': 'new', 'remote_message_id': 'm-b',
                        'date': '2026-10-02 10:00:00'})
        self.assertEqual(Conv._merge_duplicates(), 1)
        self.assertFalse(b.exists())
        self.assertEqual(a.message_ids.mapped('body'), ['new', 'old'])
        self.assertEqual((a.lid_chat_id, a.display_name_remote, a.last_message_preview, a.unread_count),
                         ('88001@lid', 'Mona', 'new', 2))

    def test_49_inbox_list_like_whatsapp(self):
        self.session.phone_number = '201001239900'
        Conv = self.env['elblasy.waha.conversation']
        me = Conv._get_or_create(self.session, '201001239900@c.us')
        photo = self._msg('pic', hasMedia=True, media={'mimetype': 'image/jpeg'})
        rows = {r['id']: r for r in Conv.with_user(self.user).inbox_conversations(session_id=self.session.id)}
        self.assertTrue(rows[me.id]['is_me'])
        row = rows[photo.conversation_id.id]
        self.assertEqual((row['last_type'], row['last_direction']), ('image', 'in'))
        unread = Conv.with_user(self.user).inbox_conversations(session_id=self.session.id, unread=True)
        self.assertEqual([r['id'] for r in unread], [photo.conversation_id.id])


    def test_50_chat_list_mirrors_the_phone(self):
        Conv = self.env['elblasy.waha.conversation']
        self.session.status = 'WORKING'
        overview = [
            {'id': '99001@lid', 'name': 'Pinned Friend', 'picture': None,
             'lastMessage': {'id': 'false_99001@lid_A', 'from': '99001@lid', 'fromMe': False, 'body': 'hi',
                             'timestamp': 1700000100, '_data': {'type': 'chat'}},
             '_chat': {'pinned': True, 'archived': False, 'isMuted': True, 'unreadCount': 3}},
            {'id': '1203555@g.us', 'name': 'Family', 'picture': None,
             'lastMessage': {'id': 'false_1203555@g.us_B', 'from': '1203555@g.us', 'participant': '2011@c.us',
                             'fromMe': False, 'body': 'dinner?', 'timestamp': 1700000200,
                             '_data': {'type': 'chat', 'notifyName': 'Mum'}},
             '_chat': {'pinned': False, 'archived': False, 'unreadCount': 1}},
            {'id': '201001239700@c.us', 'name': '+20 100 123 9700', 'picture': None, 'lastMessage': None,
             '_chat': {'archived': True, 'unreadCount': 0}},
            {'id': 'status@broadcast', 'name': 'x', '_chat': {}},
        ]
        with patch(f'{CLIENT}.get', return_value=overview), patch(f'{CLIENT}.lid_to_phone', return_value=False):
            self.assertEqual(self.session._sync_chat_list(), 3)
        friend = Conv.search([('remote_chat_id', '=', '99001@lid')])
        self.assertEqual((friend.name, friend.wa_pinned, friend.wa_muted, friend.unread_count),
                         ('Pinned Friend', True, True, 3))  # phone's unread count, not +1 for the imported message
        self.assertEqual(friend.last_message_preview, 'hi')
        family = Conv.search([('remote_chat_id', '=', '1203555@g.us')])
        self.assertEqual((family.chat_type, family.name, family.message_ids.author_name), ('group', 'Family', 'Mum'))
        archived = Conv.search([('remote_chat_id', '=', '201001239700@c.us')])
        self.assertTrue(archived.wa_archived)
        self.assertFalse(archived.display_name_remote)  # a bare number is not a name
        rows = Conv.with_user(self.user).inbox_conversations(session_id=self.session.id)
        self.assertEqual(rows[0]['id'], friend.id)  # pinned first, like WhatsApp
        self.assertNotIn(archived.id, [r['id'] for r in rows])
        self.assertEqual([r['id'] for r in Conv.with_user(self.user).inbox_conversations(
            session_id=self.session.id, kind='archived')], [archived.id])
        self.assertEqual([r['id'] for r in Conv.with_user(self.user).inbox_conversations(
            session_id=self.session.id, kind='groups')], [family.id])
        self.assertEqual(rows[1]['last_author'], 'Mum')
        # throttled: a second refresh within 30 s does not call WAHA
        with patch(f'{CLIENT}.get') as get:
            self.assertFalse(Conv.with_user(self.user).inbox_refresh(self.session.id))
        get.assert_not_called()

    def test_51_group_messages_never_trigger_auto_replies(self):
        self.env['elblasy.waha.autoreply'].create({'name': 'Hi', 'session_id': self.session.id,
                                                   'reply_text': 'Hello!', 'trigger': 'keyword', 'keywords': 'stop, hi'})
        self.session.status = 'WORKING'
        with patch(f'{CLIENT}.send_text') as send:
            self._event('message.any', {'id': 'false_1203666@g.us_C', 'from': '1203666@g.us', 'participant': '2012@c.us',
                                        'fromMe': False, 'body': 'stop', 'timestamp': 1700000000,
                                        '_data': {'type': 'chat', 'notifyName': 'Ali'}}, 'grp-c')
        send.assert_not_called()  # neither auto-reply nor the opt-out confirmation in a group
        self.assertFalse(self.env['elblasy.waha.optout'].search([]))

    def test_52_media_policy_and_download_on_demand(self):
        self.session.write({'status': 'WORKING', 'media_download': 'limit', 'media_max_mb': 1})
        payload = {'hasMedia': True, '_data': {'type': 'document', 'size': 5 * 1024 * 1024},
                   'media': {'url': 'http://waha.test/api/files/big.pdf', 'mimetype': 'application/pdf',
                             'filename': 'big.pdf'}}
        with patch(f'{CLIENT}.download') as download:
            big = self._msg('big', **payload)
        download.assert_not_called()
        self.assertTrue(big.media_pending)
        self.assertTrue(big._to_ui()['media_pending'])
        conv = big.conversation_id
        with patch(f'{CLIENT}.get', return_value={'media': payload['media']}) as get, \
                patch(f'{CLIENT}.download', return_value=b'%PDF-1.4'):
            ui = conv.with_user(self.user).inbox_download_media(big.id)
        self.assertIn('downloadMedia', get.call_args.args[1])
        self.assertEqual(ui['attachments'][0]['name'], 'big.pdf')
        self.assertFalse(big.media_pending)
        with patch(f'{CLIENT}.download', return_value=b'img') as download:
            small = self._msg('small', hasMedia=True, _data={'type': 'image', 'size': 2000},
                              media={'url': 'http://waha.test/api/files/s.jpg', 'mimetype': 'image/jpeg'})
        download.assert_called_once()
        self.assertFalse(small.media_pending)
        # retention: old files go, the message stays and can be downloaded again
        self.session.media_retention_days = 30
        self.env.cr.execute("UPDATE elblasy_waha_message SET date = now() - interval '40 days' WHERE id = %s", [small.id])
        small.invalidate_recordset()
        self.env['elblasy.waha.session']._cron_purge_media()
        self.assertFalse(small.attachment_ids)
        self.assertTrue(small.exists() and small.media_pending)

    def test_53_history_import_and_paging_by_date(self):
        self.session.status = 'WORKING'
        newest = self._msg('new1', body='newest', timestamp=1700000500)
        conv = newest.conversation_id
        conv.unread_count = 0
        older = [{'id': f'false_201001239000@c.us_old{i}', 'from': '201001239000@c.us', 'fromMe': False,
                  'body': f'old {i}', 'timestamp': 1700000000 + i, '_data': {'type': 'chat'}} for i in range(3)]
        with patch(f'{CLIENT}.get', return_value=older) as get:
            self.assertEqual(conv.with_user(self.user).inbox_fetch_history(before=fields_dt_from_ts(1700000500)), 3)
        self.assertIn('filter.timestamp.lte', get.call_args.args[1])
        self.assertEqual(conv.unread_count, 0)  # history is not unread
        page = conv.with_user(self.user).inbox_messages(before_id=newest.id)
        self.assertEqual([m['body'] for m in page['messages']], ['old 0', 'old 1', 'old 2'])
        with patch(f'{CLIENT}.get', return_value=older):
            self.assertEqual(conv.with_user(self.user).inbox_fetch_history(), 0)  # nothing twice


    def test_54_reports_filter_by_phone_and_contact(self):
        partner = self.env['res.partner'].create({'name': 'Report Customer', 'phone': '+20 100 123 9000'})
        reply_to = self._msg('rep1', body='price?')
        conv = reply_to.conversation_id
        conv.partner_id = partner
        self.env['elblasy.waha.message'].create({
            'conversation_id': conv.id, 'direction': 'out', 'body': 'It is 100', 'user_id': self.manager.id,
            'date': fields_dt_from_ts(1700000000 + 600), 'remote_message_id': 'rep-out', 'delivery_status': 'read'})
        Report = self.env['elblasy.waha.report.message'].with_user(self.manager)
        rows = Report.search([('phone', 'ilike', '1239000')])
        self.assertEqual(set(rows.mapped('direction')), {'in', 'out'})
        self.assertEqual(Report.search([('partner_id', 'child_of', partner.id)]), rows)
        human = rows.filtered(lambda r: r.direction == 'out')
        self.assertAlmostEqual(human.response_minutes, 10.0, places=1)  # 600 s after the customer's message
        self.assertFalse(rows.filtered(lambda r: r.direction == 'in').response_minutes)  # no zeros in averages
        self.assertIn(human.weekday, dict(human._fields['weekday'].selection))
        self.assertTrue(0 <= human.hour <= 23)
        groups = Report.read_group([('phone', 'ilike', '1239000')], ['user_id'], ['__count'])
        self.assertTrue(groups)
        chat = self.env['elblasy.waha.report.chat'].with_user(self.manager).search([('conversation_id', '=', conv.id)])
        self.assertEqual((chat.incoming_count, chat.outgoing_count, chat.partner_id), (1, 1, partner))
        self.assertAlmostEqual(chat.avg_response_minutes, 10.0, places=1)
        self.assertFalse(chat.waiting)  # answered
        action = chat.action_open_chat()
        self.assertEqual((action['tag'], action['context']['waha_conversation_id']), ('elblasy_waha_inbox', conv.id))
        self.assertEqual(partner.action_open_waha_conversations()['tag'], 'elblasy_waha_inbox')  # one chat: open it
        with self.assertRaises(AccessError):
            self.env['elblasy.waha.report.chat'].with_user(self.user).search([])  # reports are for managers


def fields_dt_sub_minutes(minutes):
    from odoo import fields
    return fields.Datetime.subtract(fields.Datetime.now(), minutes=minutes)


def fields_dt_from_ts(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)

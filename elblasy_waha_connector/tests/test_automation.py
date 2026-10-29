from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

CLIENT = 'odoo.addons.elblasy_waha_connector.models.waha_client.WahaClient'


@tagged('post_install', '-at_install')
class TestAutomation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'U', 'login': 'au', 'email': 'au@example.com',
            'group_ids': [(6, 0, [cls.env.ref('elblasy_waha_connector.group_waha_manager').id,
                                  cls.env.ref('base.group_partner_manager').id])]})
        server = cls.env['elblasy.waha.server'].create({'name': 'S', 'base_url': 'http://waha.test', 'api_key': 'K'})
        cls.session = cls.env['elblasy.waha.session'].create({
            'name': 'Main', 'session_name': 'main', 'server_id': server.id, 'status': 'WORKING'})
        cls.group = cls.env['elblasy.waha.group'].create({
            'name': 'Cust group', 'session_id': cls.session.id, 'chat_id': '1203@g.us', 'allow_send': True})
        cls.action = cls.env['ir.actions.server'].create({
            'name': 'Notify', 'model_id': cls.env['ir.model']._get('res.partner').id, 'state': 'waha_send',
            'waha_session_id': cls.session.id, 'waha_user_id': cls.user.id,
            'waha_body': 'Hello {{ object.name }}',
        })

    def setUp(self):
        super().setUp()
        patcher = patch(f'{CLIENT}.check_exists', return_value=(True, None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, partner):
        with patch(f'{CLIENT}.post', return_value={'id': 'true_q'}) as post:
            self.env['elblasy.waha.queue']._cron_process()
        return post

    def test_01_trigger_queues_and_sends_to_customer_and_groups(self):
        partner = self.env['res.partner'].create({
            'name': 'Acme', 'phone': '+20 100 123 4567', 'waha_group_ids': [(6, 0, self.group.ids)]})
        self.action.with_context(active_model='res.partner', active_id=partner.id, active_ids=partner.ids).run()
        item = self.env['elblasy.waha.queue'].search([('res_id', '=', partner.id)])
        self.assertEqual((item.state, item.record_name), ('pending', 'Acme'))
        post = self._run(partner)
        self.assertEqual(item.state, 'done')
        self.assertEqual(sorted(c.args[1]['chatId'] for c in post.call_args_list), ['1203@g.us', '201001234567@c.us'])
        self.assertEqual({c.args[1]['text'] for c in post.call_args_list}, {'Hello Acme'})

    def test_02_once_per_record(self):
        partner = self.env['res.partner'].create({'name': 'Once', 'phone': '+20 100 123 4568'})
        for _i in range(3):
            self.action.with_context(active_model='res.partner', active_id=partner.id, active_ids=partner.ids).run()
        self.assertEqual(self.env['elblasy.waha.queue'].search_count([('res_id', '=', partner.id)]), 1)

    def test_03_delay_and_skip(self):
        self.action.waha_delay_minutes = 30
        partner = self.env['res.partner'].create({'name': 'Late', 'phone': '+20 100 123 4569'})
        self.action.with_context(active_model='res.partner', active_id=partner.id, active_ids=partner.ids).run()
        item = self.env['elblasy.waha.queue'].search([('res_id', '=', partner.id)])
        with patch(f'{CLIENT}.post') as post:
            self.env['elblasy.waha.queue']._cron_process()
        post.assert_not_called()  # not due yet
        self.assertEqual(item.state, 'pending')
        self.action.waha_delay_minutes = 0
        nobody = self.env['res.partner'].create({'name': 'NoPhone'})
        self.action.with_context(active_model='res.partner', active_id=nobody.id, active_ids=nobody.ids).run()
        skipped = self.env['elblasy.waha.queue'].search([('res_id', '=', nobody.id)])
        skipped.scheduled_for = '2000-01-01 00:00:00'
        self._run(nobody)
        self.assertEqual(skipped.state, 'skipped')

    def test_04_failure_isolated_and_retry(self):
        from odoo.addons.elblasy_waha_connector.models.waha_client import WahaError
        partner = self.env['res.partner'].create({'name': 'Bad', 'phone': '+20 100 123 4570'})
        good = self.env['res.partner'].create({'name': 'Good', 'phone': '+20 100 123 4571'})
        for p in (partner, good):
            self.action.with_context(active_model='res.partner', active_id=p.id, active_ids=p.ids).run()

        def fake(self_, path, payload):
            if payload['chatId'].startswith('2010012345') and payload['chatId'].endswith('70@c.us'):
                raise WahaError('http', 'HTTP 400', 400)
            return {'id': 'true_ok'}
        with patch(f'{CLIENT}.post', fake):
            self.env['elblasy.waha.queue']._cron_process()
        Queue = self.env['elblasy.waha.queue']
        self.assertEqual(Queue.search([('res_id', '=', partner.id)]).state, 'failed')
        self.assertEqual(Queue.search([('res_id', '=', good.id)]).state, 'done')  # not blocked by the bad one
        failed = Queue.search([('res_id', '=', partner.id)])
        failed.action_retry()
        self.assertEqual(failed.state, 'pending')
        self._run(partner)
        self.assertEqual(failed.state, 'done')
        self.assertEqual(failed.attempts, 2)

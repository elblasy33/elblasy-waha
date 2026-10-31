import base64
from unittest.mock import patch

from odoo.tests import HttpCase, tagged

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')

# Runs in Chrome: fails on any JavaScript/template error of the inbox and checks that every kind of
# message is drawn the WhatsApp way.
CHECK_INBOX = """
const wait = async (selector, count = 1) => {
    for (let i = 0; i < 150; i++) {
        const found = document.querySelectorAll(selector);
        if (found.length >= count) {
            return found;
        }
        await new Promise((resolve) => setTimeout(resolve, 100));
    }
    throw new Error("Not found: " + selector);
};
(async () => {
    const conv = (await wait(".o_waha_conv"))[0];
    if (!conv.querySelector(".o_waha_unread_badge")) throw new Error("unread badge missing");
    if (!document.querySelector(".o_waha_chip.active")) throw new Error("filter chips missing");
    conv.click();
    await wait(".o_waha_bubble", 8);
    for (const selector of [".o_waha_day", ".o_waha_img", ".o_waha_doc .o_waha_pdf[data-icon=picture_as_pdf]", ".o_waha_location",
                            ".o_waha_vcard", ".o_waha_poll", ".o_waha_quote", ".o_waha_reactions",
                            ".o_waha_revoked", ".o_waha_pending", ".o_waha_archived_row", ".o_waha_tick_read", ".o_waha_text b", ".o_waha_space", ".o_waha_meta", ".o_waha_composer textarea"]) {
        await wait(selector);
    }
    // message menu: open it on a received message and start a reply
    document.querySelector(".o_waha_bubble.o_in .o_waha_msg_menu_btn").click();
    await wait(".o_waha_msg_menu .o_waha_quick_reactions");
    document.querySelector(".o_waha_msg_menu .o_waha_act_reply").click();
    await wait(".o_waha_composer_bar");
    document.querySelector(".o_waha_composer_bar .o_waha_icon_btn").click();
    // media viewer instead of a new tab
    document.querySelector(".o_waha_media_link").click();
    await wait(".o_waha_viewer .o_waha_viewer_media img");
    await wait(".o_waha_viewer_thumbs button.active");
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await new Promise((resolve) => setTimeout(resolve, 200));
    if (document.querySelector(".o_waha_viewer")) throw new Error("viewer did not close");
    // contact info drawer and search in the chat
    document.querySelector(".o_waha_thread_title").click();
    await wait(".o_waha_info .o_waha_info_actions");
    document.querySelector(".o_waha_info_actions button").click(); // Search
    const box = (await wait(".o_waha_info_search input"))[0];
    box.value = "team";
    box.dispatchEvent(new Event("input"));
    (await wait(".o_waha_info_body .o_waha_info_row"))[0].click();
    await wait(".o_waha_flash");
    document.querySelector(".o_waha_info_header .o_waha_icon_btn").click(); // back
    document.querySelector(".o_waha_info_header .o_waha_icon_btn").click(); // close
    await wait(".o_waha_voice");
    document.querySelector(".o_waha_chip:nth-child(3)").click(); // "Groups"
    (await wait(".o_waha_conv .o_waha_avatar_group"))[0].closest(".o_waha_conv").click();
    await wait(".o_waha_author");
    document.querySelector(".o_waha_chip:nth-child(2)").click(); // "Unread": the chat was just read
    await new Promise((resolve) => setTimeout(resolve, 500));
    console.log("test successful");
})().catch((error) => console.error(error.message));
"""


@tagged('post_install', '-at_install')
class TestInboxUi(HttpCase):

    def test_inbox_renders_like_whatsapp(self):
        admin = self.env.ref('base.user_admin')
        admin.group_ids = [(4, self.env.ref('elblasy_waha_connector.group_waha_user').id)]
        server = self.env['elblasy.waha.server'].create({'name': 'S', 'base_url': 'http://waha.test'})
        session = self.env['elblasy.waha.session'].create({
            'name': 'Sales', 'session_name': 'ui', 'server_id': server.id, 'status': 'WORKING',
            'sync_read_state': False})
        conv = self.env['elblasy.waha.conversation'].create({
            'session_id': session.id, 'remote_chat_id': '201001230000@c.us', 'remote_phone': '201001230000',
            'display_name_remote': 'Mona Ali'})
        Message = self.env['elblasy.waha.message']
        Attachment = self.env['ir.attachment']

        def message(key, **vals):
            return Message.create({'conversation_id': conv.id, 'remote_message_id': key, 'direction': 'in',
                                   'date': '2026-10-29 09:00:00', **vals})

        message('t1', body='Hello *team*, see https://elblasy.app', reactions={'2010@c.us': '❤️'})
        message('t2', body='Sure', direction='out', delivery_status='read', quoted_body='Hello team')
        photo = message('p1', message_type='image')
        Attachment.create({'name': 'p.png', 'raw': PNG, 'mimetype': 'image/png',
                           'res_model': Message._name, 'res_id': photo.id})
        doc = message('d1', message_type='document', direction='out', delivery_status='delivered')
        Attachment.create({'name': 'Quote.pdf', 'raw': b'%PDF-1.4', 'mimetype': 'application/pdf',
                           'res_model': Message._name, 'res_id': doc.id})
        message('l1', message_type='location', body='Cairo', latitude=30.04, longitude=31.23)
        message('c1', message_type='contact', body='Ali\n+20 100 123 4567')
        message('q1', message_type='poll', body='Lunch?\n• Yes\n• No')
        message('m1', message_type='video', media_pending=True)
        message('v1', message_type='audio', media_pending=True, is_voice=True, media_duration=14)
        message('r1', body='oops', is_revoked=True, date='2026-10-30 08:00:00')
        Conv = self.env['elblasy.waha.conversation']
        group = Conv.create({'session_id': session.id, 'remote_chat_id': '120300@g.us', 'display_name_remote': 'Team'})
        Message.create({'conversation_id': group.id, 'remote_message_id': 'g1', 'direction': 'in', 'body': 'hi all',
                        'author_name': 'Sara', 'sender_identifier': '2010@c.us', 'date': '2026-10-28 09:00:00'})
        Conv.create({'session_id': session.id, 'remote_chat_id': '201001230099@c.us', 'wa_archived': True})
        client = 'odoo.addons.elblasy_waha_connector.models.waha_client.WahaClient'
        with patch(f'{client}.get', return_value=[]):  # chat list mirror and history: nothing new on WhatsApp
            self.browser_js('/odoo/action-elblasy_waha_connector.waha_inbox_action', CHECK_INBOX, login='admin',
                            timeout=90)

    def test_reports_open(self):
        admin = self.env.ref('base.user_admin')
        admin.group_ids = [(4, self.env.ref('elblasy_waha_connector.group_waha_manager').id)]
        server = self.env['elblasy.waha.server'].create({'name': 'S', 'base_url': 'http://waha.test'})
        session = self.env['elblasy.waha.session'].create({'name': 'R', 'session_name': 'rep', 'server_id': server.id})
        conv = self.env['elblasy.waha.conversation'].create({
            'session_id': session.id, 'remote_chat_id': '201001230001@c.us', 'remote_phone': '201001230001'})
        self.env['elblasy.waha.message'].create({'conversation_id': conv.id, 'direction': 'in', 'body': 'hi',
                                                 'remote_message_id': 'rp1'})
        for action, selector in (('waha_report_action', '.o_list_renderer'), ('waha_report_chat_action', '.o_list_renderer'),
                                 ('waha_report_peak_action', '.o_pivot'), ('waha_report_agent_action', '.o_pivot'),
                                 ('waha_report_waiting_action', '.o_list_renderer, .o_view_nocontent'),
                                 ('waha_report_document_action', '.o_list_renderer, .o_view_nocontent'),
                                 ('waha_report_business_action', '.o_pivot, .o_view_nocontent'),
                                 ('waha_tracked_model_action', '.o_list_renderer, .o_view_nocontent'),
                                 ('waha_dashboard_action', '.o_waha_kpi')):
            code = """
                (async () => {
                    for (let i = 0; i < 150 && !document.querySelector('%s'); i++) {
                        await new Promise((r) => setTimeout(r, 100));
                    }
                    if (!document.querySelector('%s')) throw new Error('report did not render: %s');
                    console.log('test successful');
                })().catch((e) => console.error(e.message));
            """ % (selector, selector, action)
            self.browser_js(f'/odoo/action-elblasy_waha_connector.{action}', code, login='admin', timeout=60)


@tagged('post_install', '-at_install')
class TestStyles(HttpCase):

    def test_style_sheets_compile(self):
        """A SCSS error does not fail a page: Odoo silently serves the previous styles with a red banner.
        Build the bundles that contain our styles (left-to-right, right-to-left for Arabic, dark) and fail loudly."""
        IrQweb = self.env['ir.qweb']
        for name, rtl in (('web.assets_web', False), ('web.assets_web', True), ('web.assets_web_dark', False)):
            bundle = IrQweb._get_asset_bundle(name, js=False, rtl=rtl)
            bundle.preprocess_css()
            self.assertFalse(bundle.css_errors, f"{name} (rtl={rtl}): {bundle.css_errors}")

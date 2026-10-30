{
    'name': 'Elblasy WhatsApp (WAHA)',
    'version': '20.0.3.3.0',
    'author': 'Elblasy.app',
    'website': 'https://elblasy.app',
    'category': 'Productivity/Discuss',
    'summary': "WhatsApp inbox, groups and automation for Odoo, powered by your own WAHA server",
    'description': """
Elblasy WhatsApp (WAHA)
=======================
Connect Odoo to a WAHA server (https://waha.devlike.pro): WhatsApp sessions with QR login,
inbox with live messages, instant messages from contacts / employees / documents,
WhatsApp groups (sync, whitelist, send text and PDF documents), customer-linked groups
and automation rules with a throttled queue. See static/description/index.html.
""",
    'depends': ['base', 'bus', 'contacts', 'mail', 'base_automation'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/security.xml',
        'security/ir.access.csv',
        'views/waha_server_views.xml',
        'views/waha_session_views.xml',
        'views/waha_conversation_views.xml',
        'views/waha_message_views.xml',
        'views/waha_webhook_event_views.xml',
        'views/waha_group_views.xml',
        'views/waha_group_post_views.xml',
        'views/waha_group_send_views.xml',
        'views/waha_group_manage_views.xml',
        'views/res_partner_views.xml',
        'views/ir_actions_server_views.xml',
        'views/waha_queue_views.xml',
        'views/waha_report_views.xml',
        'views/menus.xml',
        'views/waha_label_call_views.xml',
        'data/ir_cron.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'elblasy_waha_connector/static/src/inbox/*',
            ('remove', 'elblasy_waha_connector/static/src/inbox/*.dark.scss'),
        ],
        'web.assets_web_dark': [
            'elblasy_waha_connector/static/src/inbox/*.dark.scss',
        ],
    },
    'icon': '/elblasy_waha_connector/static/description/icon.png',
    'post_init_hook': 'post_init_hook',
    'license': 'LGPL-3',
    'installable': True,
    'application': True,
}

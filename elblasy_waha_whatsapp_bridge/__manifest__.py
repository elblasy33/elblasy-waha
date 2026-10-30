{
    'name': 'Elblasy WAHA - Odoo WhatsApp Bridge',
    'version': '20.0.1.2.0',
    'author': 'Elblasy.app',
    'website': 'https://elblasy.app',
    'category': 'Productivity/Discuss',
    'summary': "Run Odoo's native WhatsApp app (Enterprise) through your WAHA session and message anyone without templates",
    'description': """
Odoo WhatsApp Bridge for WAHA
=============================
Add-on of "Elblasy WhatsApp (WAHA)" for Odoo Enterprise. Links a WAHA session to a
``whatsapp.account`` and redirects the transport layer of the native ``whatsapp`` app
(send, media, webhooks) to WAHA. Adds New Chat (Discuss) and free-text messages without
templates. See static/description/index.html.
""",
    'depends': ['whatsapp', 'whatsapp_identifiers', 'elblasy_waha_connector'],
    'data': [
        'security/ir.access.csv',
        'views/waha_session_views.xml',
        'views/whatsapp_account_views.xml',
        'views/waha_new_chat_views.xml',
        'views/whatsapp_composer_views.xml',
    ],
    'icon': '/elblasy_waha_whatsapp_bridge/static/description/icon.png',
    'post_load': 'post_load',
    'license': 'LGPL-3',
    'installable': True,
    'application': False,
}

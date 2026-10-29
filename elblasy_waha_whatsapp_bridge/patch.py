"""Redirect the transport layer of the native whatsapp app to WAHA for accounts bound to a WAHA session.

Accounts without a WAHA session keep the original (Meta) behaviour untouched.
"""
import functools

from odoo.addons.elblasy_waha_connector.models.waha_client import WahaError


def post_load():
    from odoo.addons.whatsapp.tools.whatsapp_api import WhatsAppApi
    from odoo.addons.whatsapp.tools.whatsapp_exception import WhatsAppError
    from .tools import transport

    def waha_only(name, replacement):
        original = getattr(WhatsAppApi, name)

        @functools.wraps(original)
        def wrapper(self, *args, **kwargs):
            session = transport.waha_session_of(self)
            if session:
                return replacement(self, session, *args, **kwargs)
            return original(self, *args, **kwargs)
        setattr(WhatsAppApi, name, wrapper)

    def _send(self, session, bsuid, number, message_type, send_vals, parent_message_id=False):
        return transport.send(self, bsuid, number, message_type, send_vals, parent_message_id)

    def _upload(self, session, attachment):
        return f"{transport.ATT_PREFIX}{attachment.id}"

    def _download(self, session, document_id):
        try:
            return session.server_id._get_client().download(document_id)
        except WahaError as e:
            raise transport.to_waha_error(e)

    def _test(self, session):
        try:
            session.server_id._get_client().get_session(session.session_name)
        except WahaError as e:
            raise WhatsAppError(str(e), failure_type='account')

    def _phone(self, session, phone_uid):
        return session.phone_number or False

    def _templates(self, session, fetch_all=False):
        return {'data': []}  # templates are managed locally; there is nothing to fetch

    def _submit_new(self, session, json_data):
        import json
        return {'id': f"waha-{session.id}-{json.loads(json_data)['name']}", 'status': 'APPROVED'}

    def _submit_update(self, session, json_data, wa_template_uid):
        return True

    def _template_data(self, session, wa_template_uid):
        return {}

    def _demo_document(self, session, attachment):
        return f"{transport.ATT_PREFIX}{attachment.id}"

    def _block(self, session, operation, numbers):
        return {'updated_numbers': [], 'failures': {
            n: "Blocking is not supported through WAHA." for n in numbers}}

    waha_only('_send_whatsapp_to_identifier', _send)
    waha_only('_upload_whatsapp_document', _upload)
    waha_only('_get_whatsapp_document', _download)
    waha_only('_test_connection', _test)
    waha_only('_get_phone_number', _phone)
    waha_only('_get_all_template', _templates)
    waha_only('_submit_template_new', _submit_new)
    waha_only('_submit_template_update', _submit_update)
    waha_only('_get_template_data', _template_data)
    waha_only('_upload_demo_document', _demo_document)
    waha_only('_update_block_status', _block)

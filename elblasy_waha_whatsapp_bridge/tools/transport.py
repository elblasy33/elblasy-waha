"""Translation between the Meta Cloud API shapes used by the native ``whatsapp`` app and WAHA."""
import base64
import mimetypes
import re

from odoo.addons.elblasy_waha_connector.models.waha_client import WahaError
from odoo.addons.elblasy_waha_connector.models.waha_message import extract_message_id
from odoo.addons.whatsapp.tools.whatsapp_exception import WhatsAppError

ATT_PREFIX = 'waha-att:'


def waha_session_of(api):
    """The WAHA session bound to the account of this WhatsAppApi, or empty."""
    account = api.wa_account_id
    if not account or 'waha_session_id' not in account._fields or not account.sudo().active:
        return None
    return account.sudo().waha_session_id


def bsuid_of(lid):
    """Odoo's BSUID format is ``<country>.<id>``; the country of a LID is unknown (ZZ)."""
    return f"ZZ.{lid}"


def digits(number):
    return re.sub(r'\D', '', number or '')


def to_waha_error(error):
    kind = 'network' if error.kind in ('network', 'response') else False
    return WhatsAppError(str(error), failure_type=kind or 'unknown')


def render_template_text(env, account, send_vals):
    """WAHA has no Meta templates: render the approved template locally into plain text."""
    template = env['whatsapp.template'].sudo().search([
        ('template_name', '=', send_vals.get('name')), ('wa_account_id', '=', account.id)], limit=1)
    components = {c['type']: c for c in send_vals.get('components', []) if c.get('type') in ('header', 'body')}

    def fill(text, parameters):
        for index, param in enumerate(parameters or [], 1):
            key = param.get('parameter_name') or str(index)
            text = text.replace('{{%s}}' % key, param.get('text') or '')
        return text

    parts = []
    header = components.get('header')
    if template.header_type == 'text' and template.header_text:
        parts.append('*%s*' % fill(template.header_text, (header or {}).get('parameters')))
    parts.append(fill(template.body or '', (components.get('body') or {}).get('parameters')))
    if template.footer_text:
        parts.append(template.footer_text)
    buttons = [b for b in template.button_ids if b.button_type == 'url' and getattr(b, 'website_url', False)]
    parts += [f"{b.name}: {b.website_url}" for b in buttons]
    return '\n\n'.join(p for p in parts if p)


def render_interactive_text(send_vals):
    """Interactive messages (buttons/lists) are unsupported by WAHA/WEBJS: send them as numbered text."""
    lines = []
    if send_vals.get('header', {}).get('text'):
        lines.append('*%s*' % send_vals['header']['text'])
    if send_vals.get('body', {}).get('text'):
        lines.append(send_vals['body']['text'])
    action = send_vals.get('action') or {}
    options = [b.get('reply', {}).get('title') for b in action.get('buttons', [])]
    for section in action.get('sections', []):
        options += [row.get('title') for row in section.get('rows', [])]
    if options:
        lines.append('\n'.join(f"{i}. {title}" for i, title in enumerate([o for o in options if o], 1)))
    if action.get('parameters', {}).get('url'):
        lines.append(f"{action['parameters'].get('display_text', '')} {action['parameters']['url']}".strip())
    if send_vals.get('footer', {}).get('text'):
        lines.append(send_vals['footer']['text'])
    return '\n\n'.join(lines)


def template_header_media(send_vals):
    """``(kind, media_vals)`` of the media header of a template message, if any."""
    for component in send_vals.get('components', []):
        if component.get('type') != 'header':
            continue
        for param in component.get('parameters', []):
            if param.get('type') in ('image', 'video', 'document') and isinstance(param.get(param['type']), dict):
                return param['type'], param[param['type']]
    return None


def _media_file(env, media):
    if media.get('link'):
        return {'url': media['link']}, media.get('filename')
    ref = media.get('id') or ''
    if not ref.startswith(ATT_PREFIX):
        raise WhatsAppError("Unsupported media reference.", failure_type='unknown')
    attachment = env['ir.attachment'].sudo().browse(int(ref[len(ATT_PREFIX):])).exists()
    if not attachment:
        raise WhatsAppError("Attachment not found.", failure_type='unknown')
    filename = attachment.name or 'file'
    return {
        'mimetype': attachment.mimetype or mimetypes.guess_type(filename)[0] or 'application/octet-stream',
        'filename': filename,
        'data': base64.b64encode(attachment.raw).decode(),
    }, filename


def send(api, bsuid, number, message_type, send_vals, parent_message_id=False):
    session = waha_session_of(api)
    env = api.wa_account_id.env
    client = session.server_id._get_client()
    chat_id = f"{digits(number)}@c.us" if number else (f"{bsuid.rsplit('.', 1)[-1]}@lid" if bsuid else False)
    if not chat_id or chat_id == '@c.us':
        raise WhatsAppError("Missing recipient.", failure_type='unknown')
    base = {'session': session.session_name, 'chatId': chat_id}
    if parent_message_id:
        base['reply_to'] = parent_message_id
    try:
        if message_type == 'reaction':
            client.put('/api/reaction', {
                'session': session.session_name, 'messageId': send_vals['message_id'],
                'reaction': send_vals.get('emoji', '')})
            return {'msg_uid': send_vals['message_id'], 'wa_id': digits(number)}
        if message_type in ('text', 'template', 'interactive'):
            header_media = None
            if message_type == 'text':
                text = send_vals.get('body', '')
            elif message_type == 'template':
                text = render_template_text(env, api.wa_account_id, send_vals)
                header_media = template_header_media(send_vals)
            else:
                text = render_interactive_text(send_vals)
            if header_media:
                # template with an image/video/document header: send the file with the text as caption
                kind, media = header_media
                file, _filename = _media_file(env, media)
                path = {'image': '/api/sendImage', 'video': '/api/sendVideo'}.get(kind, '/api/sendFile')
                response = client.post(path, {**base, 'file': file, 'caption': text})
            else:
                response = client.post('/api/sendText', {**base, 'text': text, 'linkPreview': True})
        elif message_type in ('image', 'video', 'audio', 'document'):
            media = send_vals
            file, _filename = _media_file(env, media)
            caption = media.get('caption')
            path = {'image': '/api/sendImage', 'video': '/api/sendVideo', 'document': '/api/sendFile',
                    'audio': '/api/sendFile'}[message_type]
            payload = {**base, 'file': file}
            if caption:
                payload['caption'] = caption
            response = client.post(path, payload)
        else:
            raise WhatsAppError(f"Unsupported message type: {message_type}", failure_type='unknown')
    except WahaError as e:
        raise to_waha_error(e)
    msg_uid = extract_message_id(response)
    if not msg_uid:
        raise WhatsAppError("WAHA did not return a message id.", failure_type='unknown')
    return {'msg_uid': msg_uid, 'wa_id': digits(number)}


# ----------------------------------------------------------------------
# inbound: WAHA webhook payload -> Meta "value" dict understood by whatsapp.account._process_messages
# ----------------------------------------------------------------------
ACK_STATUS = {1: 'sent', 2: 'delivered', 3: 'read', 4: 'read'}
THUMBNAIL_PREFIXES = ('/9j/', 'iVBORw0KGgo', 'UklGR')  # base64 of JPEG, PNG, WEBP


def looks_like_thumbnail(text):
    """WEBJS puts a base64 preview in ``body`` for media without caption: never show it as text."""
    return isinstance(text, str) and len(text) > 100 and text.startswith(THUMBNAIL_PREFIXES) and ' ' not in text


def _sender_identifiers(chat, resolve_lid=None):
    """``(number, lid)`` for a WAHA chat id. LID chats are resolved to a phone when WAHA knows it."""
    local, _sep, suffix = (chat or '').rpartition('@')
    if suffix in ('c.us', 's.whatsapp.net') and digits(local):
        return digits(local), None
    if suffix == 'lid' and digits(local):
        number = None
        if resolve_lid:
            try:
                number = resolve_lid(chat)
            except WahaError:
                number = None
        return number, (None if number else digits(local))
    return None, None


def _contact(number, lid, name):
    contact = {'profile': {'name': name or number or lid}}
    if number:
        contact['wa_id'] = number
    if lid:
        contact['user_id'] = bsuid_of(lid)
    return contact


def meta_value_from_message(account, payload, resolve_lid=None):
    """Return the Meta-shaped value for an incoming WAHA ``message.any`` payload, or None if unsupported."""
    number, lid = _sender_identifiers(payload.get('from'), resolve_lid)
    if not number and not lid:
        return None  # groups / status / channels
    data = payload.get('_data') if isinstance(payload.get('_data'), dict) else {}
    message = {'id': payload['id'], 'timestamp': str(int(payload.get('timestamp') or 0))}
    if number:
        message['from'] = number
    else:
        message['from_user_id'] = bsuid_of(lid)
    reply = payload.get('replyTo')
    reply_id = reply.get('id') if isinstance(reply, dict) else reply
    if isinstance(reply_id, str) and reply_id:
        message['context'] = {'id': reply_id}
    media = payload.get('media') if isinstance(payload.get('media'), dict) else {}
    mimetype = (media.get('mimetype') or '').split(';')[0]
    if payload.get('hasMedia') and media.get('url'):
        kind = {'image': 'image', 'video': 'video', 'audio': 'audio'}.get(mimetype.split('/')[0], 'document')
        if mimetype == 'image/webp':
            kind = 'sticker'
        message['type'] = kind
        message[kind] = {'id': media['url'], 'mime_type': mimetype or 'application/octet-stream',
                         'filename': media.get('filename') or None,
                         'caption': None if looks_like_thumbnail(payload.get('body')) else payload.get('body') or None,
                         'voice': bool(data.get('type') == 'ptt')}
    elif isinstance(payload.get('location'), dict) and payload['location'].get('latitude') is not None:
        loc = payload['location']
        message['type'] = 'location'
        message['location'] = {'latitude': loc['latitude'], 'longitude': loc['longitude'],
                               'name': loc.get('name'), 'address': loc.get('address')}
    else:
        body = payload.get('body') or ''
        if looks_like_thumbnail(body):
            body = ''
        body = body or ('[unsupported content]' if payload.get('hasMedia') else '')
        if not body:
            return None
        message['type'] = 'text'
        message['text'] = {'body': body}
    return {
        'messaging_product': 'whatsapp',
        'metadata': {'display_phone_number': account.phone_number or '', 'phone_number_id': account.phone_uid},
        'contacts': [_contact(number, lid, data.get('notifyName') or data.get('pushName'))],
        'messages': [message],
    }


def meta_value_from_reaction(account, payload, resolve_lid=None):
    number, lid = _sender_identifiers(payload.get('from'), resolve_lid)
    reaction = payload.get('reaction') if isinstance(payload.get('reaction'), dict) else {}
    if (not number and not lid) or not reaction.get('messageId'):
        return None
    message = {'id': payload.get('id') or '', 'timestamp': str(int(payload.get('timestamp') or 0)),
               'type': 'reaction',
               'reaction': {'message_id': reaction['messageId'], 'emoji': reaction.get('text') or ''}}
    message['from' if number else 'from_user_id'] = number or bsuid_of(lid)
    return {
        'messaging_product': 'whatsapp',
        'metadata': {'phone_number_id': account.phone_uid},
        'contacts': [_contact(number, lid, number or lid)],
        'messages': [message],
    }

from odoo import models


class WahaReportDocument(models.Model):
    _inherit = 'elblasy.waha.report.document'

    def _document_sources(self):
        """Documents sent with Odoo's WhatsApp templates (through WAHA or Meta) are counted too."""
        return super()._document_sources() + ["""
            SELECT wm.id * 10 + 2 AS id, 1 AS nbr, wm.create_date AS date, 'template' AS source,
                   'customer' AS channel, mm.model AS res_model, mm.res_id, wt.template_name AS template,
                   (SELECT r.res_partner_id FROM mail_message_res_partner_rel r
                     WHERE r.mail_message_id = mm.id ORDER BY r.res_partner_id LIMIT 1) AS partner_id,
                   wm.mobile_number AS phone, wa.waha_session_id AS session_id, wm.create_uid AS user_id,
                   NULL::int AS company_id,
                   CASE WHEN wm.state IN ('error', 'bounced', 'cancel') THEN 'failed'
                        WHEN wm.state = 'outgoing' THEN 'pending' ELSE 'sent' END AS state
              FROM whatsapp_message wm
              JOIN whatsapp_template wt ON wt.id = wm.wa_template_id
              LEFT JOIN mail_message mm ON mm.id = wm.mail_message_id
              LEFT JOIN whatsapp_account wa ON wa.id = wm.wa_account_id
             WHERE wm.message_type = 'outbound'
        """]

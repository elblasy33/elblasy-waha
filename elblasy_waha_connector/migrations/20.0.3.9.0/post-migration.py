def migrate(cr, version):
    """Messages imported before: WEBJS base64 picture previews stored as text or as quoted text."""
    previews = r"^(/9j/|iVBORw0KGgo|UklGR)[A-Za-z0-9+/=]{60}"
    cr.execute("UPDATE elblasy_waha_message SET quoted_body = %s WHERE quoted_body ~ %s", ['📷 Photo', previews])
    cr.execute("UPDATE elblasy_waha_message SET body = NULL WHERE body ~ %s", [previews])

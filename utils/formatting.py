import re

MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!"


def escape_markdown_v2(text):
    if text is None:
        return ""
    pattern = f"([{re.escape(MDV2_SPECIALS)}])"
    return re.sub(pattern, r"\\\1", str(text))

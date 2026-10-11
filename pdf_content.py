"""Validation and normalization for generate_pdf (resume and document templates).

The model supplies structured content; this module turns whatever it sent into
one clean shape the templates can rely on. It is deliberately forgiving about
*shape* (a bullet list sent as one newline-separated string, skills as a flat
list, dates as start/end or a single string) and strict about *meaning*: text
is never reworded, only trimmed, so a visual redesign can't silently rewrite
someone's resume.
"""
import re
from urllib.parse import urlparse


class ContentError(ValueError):
    """Raised with a message naming the exact field, so a model can fix it in one retry."""


MAX_TEXT = 4000
MAX_ITEMS = 200
MAX_BLOCKS = 500
MAX_TABLE_CELLS = 2000

_BULLET_PREFIX = re.compile(r"^\s*(?:[-*•▪◦‣–—]|\d{1,2}[.)])\s+")
_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _text(value, path, required=False):
    if value is None:
        if required:
            raise ContentError(f"{path} is required")
        return ""
    if isinstance(value, bool) or isinstance(value, (list, dict)):
        raise ContentError(f"{path} must be text, got {type(value).__name__}")
    text = str(value).strip()
    if required and not text:
        raise ContentError(f"{path} is required and was empty")
    return text[:MAX_TEXT]


def _lines(value, path):
    """A list of non-empty text items; also accepts one newline-separated string."""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list):
        raise ContentError(f"{path} must be a list of text items")
    out = []
    for i, item in enumerate(value[:MAX_ITEMS]):
        text = _BULLET_PREFIX.sub("", _text(item, f"{path}[{i}]")).strip()
        if text:
            out.append(text)
    return out


def _dict_list(value, path):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ContentError(f"{path} must be a list")
    out = []
    for i, item in enumerate(value[:MAX_ITEMS]):
        if not isinstance(item, dict):
            raise ContentError(f"{path}[{i}] must be an object, got {type(item).__name__}")
        out.append(item)
    return out


def _pick(entry, *names):
    for name in names:
        if entry.get(name) not in (None, ""):
            return entry[name]
    return None


def _dates(entry, path):
    explicit = _text(_pick(entry, "dates", "date", "period"), f"{path}.dates")
    if explicit:
        return explicit
    start = _text(_pick(entry, "start", "from"), f"{path}.start")
    end = _text(_pick(entry, "end", "to"), f"{path}.end")
    if start and end:
        return f"{start} – {end}"
    return start or end


def clean_url(raw):
    """Only web/mail/phone links survive; anything else (javascript:, file:, data:) is dropped."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "@" in raw and "://" not in raw and not raw.lower().startswith(("mailto:", "tel:")):
        return "mailto:" + raw
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", raw):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in ("http", "https", "mailto", "tel"):
        return ""
    return raw


def _display_url(url):
    return re.sub(r"^(https?://(www\.)?|mailto:|tel:)", "", url).rstrip("/")


def _links(value, path):
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list):
        raise ContentError(f"{path} must be a list")
    out = []
    for i, item in enumerate(value[:MAX_ITEMS]):
        if isinstance(item, dict):
            url = clean_url(_text(_pick(item, "url", "href", "link"), f"{path}[{i}].url"))
            label = _text(item.get("label") or item.get("name"), f"{path}[{i}].label")
        else:
            url = clean_url(_text(item, f"{path}[{i}]"))
            label = ""
        if url:
            out.append({"url": url, "label": label or _display_url(url)})
    return out


def _skills(value, path):
    """Always returns [{"group": str, "items": [str]}]."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [part for part in re.split(r"[,\n;]", value)]
    if isinstance(value, dict):
        value = [{"group": group, "items": items} for group, items in value.items()]
    if not isinstance(value, list):
        raise ContentError(f"{path} must be a list")
    groups, loose = [], []
    for i, item in enumerate(value[:MAX_ITEMS]):
        if isinstance(item, dict):
            items = _lines(item.get("items") if item.get("items") is not None else item.get("skills"), f"{path}[{i}].items")
            if items:
                groups.append({"group": _text(item.get("group") or item.get("name") or item.get("category"), f"{path}[{i}].group"), "items": items})
        else:
            text = _text(item, f"{path}[{i}]")
            if text:
                loose.append(text)
    if loose:
        groups.insert(0, {"group": "", "items": loose})
    return groups


def normalize_resume(content):
    if not isinstance(content, dict):
        raise ContentError("content must be an object with the resume fields (name, experience, ...)")
    contact_in = content.get("contact") if isinstance(content.get("contact"), dict) else {}
    email = _text(_pick(contact_in, "email") or content.get("email"), "contact.email")
    phone = _text(_pick(contact_in, "phone") or content.get("phone"), "contact.phone")
    links = _links(_pick(contact_in, "links") or content.get("links"), "contact.links")
    for key in ("linkedin", "website", "github", "portfolio"):
        extra = _pick(contact_in, key) or content.get(key)
        if extra:
            links += _links({"label": key.capitalize() if key != "linkedin" else "LinkedIn", "url": extra}, f"contact.{key}")

    experience = []
    for i, e in enumerate(_dict_list(content.get("experience"), "experience")):
        title = _text(_pick(e, "title", "role", "position"), f"experience[{i}].title")
        company = _text(_pick(e, "company", "employer", "organization"), f"experience[{i}].company")
        if not (title or company):
            raise ContentError(f"experience[{i}] needs a title or company")
        experience.append({
            "title": title, "company": company,
            "location": _text(e.get("location"), f"experience[{i}].location"),
            "dates": _dates(e, f"experience[{i}]"),
            "bullets": _lines(_pick(e, "bullets", "highlights", "responsibilities", "description"), f"experience[{i}].bullets"),
        })

    education = []
    for i, e in enumerate(_dict_list(content.get("education"), "education")):
        degree = _text(_pick(e, "degree", "title", "program"), f"education[{i}].degree")
        school = _text(_pick(e, "school", "institution", "university"), f"education[{i}].school")
        if not (degree or school):
            raise ContentError(f"education[{i}] needs a degree or school")
        education.append({
            "degree": degree, "school": school,
            "location": _text(e.get("location"), f"education[{i}].location"),
            "dates": _dates(e, f"education[{i}]"),
            "details": _lines(_pick(e, "details", "bullets", "notes"), f"education[{i}].details"),
        })

    certifications = []
    raw_certs = content.get("certifications")
    if isinstance(raw_certs, str):
        raw_certs = raw_certs.splitlines()
    for i, c in enumerate((raw_certs or [])[:MAX_ITEMS] if isinstance(raw_certs, list) else []):
        if isinstance(c, dict):
            name = _text(_pick(c, "name", "title"), f"certifications[{i}].name")
            issuer = _text(_pick(c, "issuer", "organization", "authority"), f"certifications[{i}].issuer")
            date = _text(_pick(c, "date", "dates"), f"certifications[{i}].date")
        else:
            name, issuer, date = _text(c, f"certifications[{i}]"), "", ""
        if name:
            certifications.append({"name": name, "issuer": issuer, "date": date})

    languages = []
    for i, lang in enumerate((content.get("languages") if isinstance(content.get("languages"), list) else _lines(content.get("languages"), "languages"))[:MAX_ITEMS]):
        if isinstance(lang, dict):
            name = _text(_pick(lang, "language", "name"), f"languages[{i}]")
            level = _text(_pick(lang, "level", "proficiency"), f"languages[{i}].level")
            lang = f"{name} ({level})" if name and level else name
        else:
            lang = _text(lang, f"languages[{i}]")
        if lang:
            languages.append(lang)

    sections = []
    for i, s in enumerate(_dict_list(content.get("sections"), "sections")):
        title = _text(s.get("title"), f"sections[{i}].title", required=True)
        sections.append({
            "title": title,
            "text": _text(_pick(s, "text", "content"), f"sections[{i}].text"),
            "items": _lines(_pick(s, "items", "bullets"), f"sections[{i}].items"),
        })

    return {
        "name": _text(content.get("name"), "name", required=True),
        "headline": _text(_pick(content, "headline", "title", "tagline"), "headline"),
        "location": _text(_pick(contact_in, "location", "address") or content.get("location"), "contact.location"),
        "phone": phone,
        "email": email,
        "email_url": clean_url(email) if email else "",
        "phone_url": ("tel:" + re.sub(r"[^\d+]", "", phone)) if re.search(r"\d", phone) else "",
        "links": links,
        "summary": _text(_pick(content, "summary", "objective", "profile"), "summary"),
        "experience": experience,
        "education": education,
        "skills": _skills(content.get("skills"), "skills"),
        "certifications": certifications,
        "languages": languages,
        "sections": sections,
    }


_BLOCK_TYPES = {"heading", "paragraph", "bullets", "numbered", "table", "callout", "divider", "page_break"}


def normalize_document(content):
    if not isinstance(content, dict):
        raise ContentError("content must be an object with title and blocks")
    raw_blocks = content.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise ContentError("blocks must be a non-empty list of {type, ...} objects")
    blocks, cells = [], 0
    for i, b in enumerate(raw_blocks[:MAX_BLOCKS]):
        if isinstance(b, str):
            b = {"type": "paragraph", "text": b}
        if not isinstance(b, dict):
            raise ContentError(f"blocks[{i}] must be an object, got {type(b).__name__}")
        kind = _text(b.get("type"), f"blocks[{i}].type").lower().replace("-", "_") or "paragraph"
        if kind in ("list", "bullet", "ul"):
            kind = "bullets"
        elif kind in ("ordered", "ol", "numbered_list"):
            kind = "numbered"
        elif kind in ("text", "p"):
            kind = "paragraph"
        elif kind in ("h1", "h2", "h3", "title", "section"):
            b = {**b, "level": int(kind[1]) if kind[0] == "h" else b.get("level", 2)}
            kind = "heading"
        if kind not in _BLOCK_TYPES:
            raise ContentError(f"blocks[{i}].type '{kind}' is not one of: {', '.join(sorted(_BLOCK_TYPES))}")
        block = {"type": kind}
        if kind == "heading":
            try:
                level = int(b.get("level", 2))
            except (TypeError, ValueError):
                level = 2
            block.update(text=_text(b.get("text"), f"blocks[{i}].text", required=True), level=min(max(level, 1), 3))
        elif kind == "paragraph":
            block["text"] = _text(b.get("text"), f"blocks[{i}].text", required=True)
        elif kind in ("bullets", "numbered"):
            items = _lines(_pick(b, "items", "text"), f"blocks[{i}].items")
            if not items:
                raise ContentError(f"blocks[{i}].items must contain at least one item")
            block["items"] = items
        elif kind == "callout":
            block.update(title=_text(b.get("title"), f"blocks[{i}].title"), text=_text(b.get("text"), f"blocks[{i}].text", required=True))
        elif kind == "table":
            rows = b.get("rows")
            if not isinstance(rows, list) or not rows:
                raise ContentError(f"blocks[{i}].rows must be a non-empty list of rows")
            headers = [_text(h, f"blocks[{i}].headers[{j}]") for j, h in enumerate(b.get("headers") or [])]
            table_rows = []
            for r, row in enumerate(rows):
                if isinstance(row, dict):
                    row = [row.get(h, "") for h in headers] if headers else list(row.values())
                if not isinstance(row, list):
                    raise ContentError(f"blocks[{i}].rows[{r}] must be a list of cells")
                table_rows.append([_text(c, f"blocks[{i}].rows[{r}][{c_i}]") for c_i, c in enumerate(row)])
            width = max([len(headers)] + [len(r) for r in table_rows])
            cells += width * (len(table_rows) + 1)
            if cells > MAX_TABLE_CELLS:
                raise ContentError(f"tables are limited to {MAX_TABLE_CELLS} cells in total")
            block.update(
                headers=headers + [""] * (width - len(headers)) if headers else [],
                rows=[r + [""] * (width - len(r)) for r in table_rows],
                caption=_text(b.get("caption"), f"blocks[{i}].caption"),
            )
        blocks.append(block)
    return {
        "title": _text(content.get("title"), "title", required=True),
        "subtitle": _text(content.get("subtitle"), "subtitle"),
        "byline": _text(_pick(content, "byline", "author", "date"), "byline"),
        "blocks": blocks,
    }


def normalize_hex(value):
    """'#abc' / 'abc' / '#aabbcc' -> '#aabbcc', or raises."""
    match = _HEX.match((value or "").strip())
    if not match:
        raise ContentError("style.accent must be a hex color like #2f6bd0")
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return "#" + digits.lower()

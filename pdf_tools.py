"""Athena's PDF tool: one generate_pdf covering resumes and general documents.

The model supplies structured content; templates (pdf_assets/templates) and the
renderer (pdf_render.py) own layout. See pdf_content.py for how content is
validated and pdf_render.py for how pages are produced. Every PDF is served from
generated_files/ by main.py's /api/generated-files route (the download link in chat);
when a workspace is active the PDF and the spec that made it are also saved there.
"""
import json
import os
import re

from logging_setup import logger
import backup_tools
import file_tools
import generated_files
import pdf_content
import pdf_render

_STYLE_SCHEMA = {
    "type": "object",
    "description": "Optional look. Defaults are a sensible navy/modern design.",
    "properties": {
        "palette": {"type": "string", "enum": list(pdf_render.PALETTES), "description": "Color scheme."},
        "accent": {"type": "string", "description": "Custom accent color as hex (e.g. #2f6bd0); overrides palette."},
        "font": {"type": "string", "enum": list(pdf_render.FONT_SETS), "description": "modern = sans; classic = serif; editorial = serif display headings."},
        "page_size": {"type": "string", "enum": list(pdf_render.PAGE_SIZES)},
    },
}
_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": _STR}

_TEMPLATES = list(pdf_render.RESUME_TEMPLATES) + ["document"]

PDF_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "generate_pdf",
            "description": (
                "Create a designed, print-ready PDF from structured content: a resume, or any document (report, "
                "letter, memo, proposal, one-pager). You supply the text; layout, fonts, page breaks and fitting a "
                "resume to one page are handled for you, so there is no HTML or CSS to write. Pick the template and "
                "palette that suit the person or purpose. When redesigning an existing document, keep the user's "
                "wording and facts exactly as given. Returns a download link; with a workspace, the PDF and its "
                "source spec (<name>.pdf.json) are also saved there so you can revise instead of starting over."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "template": {
                        "type": "string",
                        "enum": _TEMPLATES,
                        "description": (
                            "Resumes: sidebar (dark side column with contact/skills), banner (header band plus tinted "
                            "side column), classic (single column, most ATS-friendly). document: anything else, built from blocks."
                        ),
                    },
                    "content": {
                        "type": "object",
                        "description": "Resume templates use name..sections. The document template uses title, subtitle, byline, blocks.",
                        "properties": {
                            "name": _STR,
                            "headline": {"type": "string", "description": "Short title line under the name."},
                            "contact": {
                                "type": "object",
                                "properties": {
                                    "location": _STR, "phone": _STR, "email": _STR,
                                    "links": {"type": "array", "items": {"type": "object", "properties": {"label": _STR, "url": _STR}}},
                                },
                            },
                            "summary": _STR,
                            "experience": {"type": "array", "items": {"type": "object", "properties": {
                                "title": _STR, "company": _STR, "location": _STR, "start": _STR, "end": _STR, "bullets": _STR_LIST}}},
                            "education": {"type": "array", "items": {"type": "object", "properties": {
                                "degree": _STR, "school": _STR, "location": _STR, "start": _STR, "end": _STR, "details": _STR_LIST}}},
                            "skills": {"type": "array", "items": {"type": "object", "properties": {"group": _STR, "items": _STR_LIST}}},
                            "certifications": {"type": "array", "items": {"type": "object", "properties": {"name": _STR, "issuer": _STR, "date": _STR}}},
                            "languages": _STR_LIST,
                            "sections": {"type": "array", "description": "Extra resume sections (projects, volunteering, ...).", "items": {"type": "object", "properties": {"title": _STR, "text": _STR, "items": _STR_LIST}}},
                            "title": _STR,
                            "subtitle": _STR,
                            "byline": {"type": "string", "description": "Author/date line."},
                            "blocks": {"type": "array", "items": {"type": "object", "properties": {
                                "type": {"type": "string", "enum": ["heading", "paragraph", "bullets", "numbered", "table", "callout", "divider", "page_break"]},
                                "text": _STR,
                                "level": {"type": "integer", "description": "Heading level 1-3."},
                                "items": _STR_LIST,
                                "headers": _STR_LIST,
                                "rows": {"type": "array", "items": _STR_LIST},
                                "title": {"type": "string", "description": "Callout label."},
                                "caption": _STR,
                            }, "required": ["type"]}},
                        },
                    },
                    "style": _STYLE_SCHEMA,
                    "fit": {"type": "string", "enum": ["one_page", "natural"], "description": "Resumes only. one_page (default) tightens spacing slightly to fit a single page when it can; natural never does."},
                    "filename": {"type": "string", "description": "Download name without extension."},
                },
                "required": ["template", "content"],
            },
        },
    },
]


def _sanitize_filename(name, fallback):
    base = os.path.basename(str(name or "")).strip()
    base = re.sub(r"\.pdf$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"[^A-Za-z0-9 _-]", "", base).strip().replace(" ", "_")
    return base or fallback


def _parse_style(style, default_font):
    """-> (accent_hex, font_set, page_size); raises ContentError naming the bad field."""
    style = style if isinstance(style, dict) else {}
    palette = str(style.get("palette") or pdf_render.DEFAULT_PALETTE).strip().lower()
    if style.get("accent"):
        accent = pdf_content.normalize_hex(str(style["accent"]))
    elif palette in pdf_render.PALETTES:
        accent = pdf_render.PALETTES[palette]
    else:
        raise pdf_content.ContentError(f"style.palette '{palette}' is not one of: {', '.join(pdf_render.PALETTES)}")
    font = str(style.get("font") or default_font).strip().lower()
    if font not in pdf_render.FONT_SETS:
        raise pdf_content.ContentError(f"style.font '{font}' is not one of: {', '.join(pdf_render.FONT_SETS)}")
    page = str(style.get("page_size") or "letter").strip().lower()
    if page not in pdf_render.PAGE_SIZES:
        raise pdf_content.ContentError(f"style.page_size '{page}' is not one of: {', '.join(pdf_render.PAGE_SIZES)}")
    return accent, font, page


def _content_arg(args, control_keys):
    """`content` is the documented place for the data, but a model that flattens the
    fields into the top level of the call still gets a working result."""
    content = args.get("content")
    if content is None:
        content = {k: v for k, v in args.items() if k not in control_keys}
    return content


def _store(pdf_bytes, filename):
    return generated_files.store(pdf_bytes, filename + ".pdf")


def _render(template_file, data, args, default_font, fit_steps, label):
    accent, font, page = _parse_style(args.get("style"), default_font)
    html = pdf_render.render_html(template_file, data, accent, font, page)
    pdf, pages, step = pdf_render.render_pdf(html, fit_steps)
    filename = _sanitize_filename(args.get("filename"), label)
    token = _store(pdf, filename)
    result = {
        "filename": f"{filename}.pdf",
        "url": f"/api/generated-files/{token}",
        "size": len(pdf),
        "pages": pages,
    }
    return result, step


def _guarded(fn, args):
    if not isinstance(args, dict):
        return {"error": "Arguments must be an object."}
    try:
        return fn(args)
    except pdf_content.ContentError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.warning(f"{fn.__name__} failed: {e}")
        return {"error": f"PDF generation failed: {e}"}


def _resume(args, template):
    content = _content_arg(args, {"template", "style", "fit", "filename", "content"})
    if isinstance(content, dict) and "blocks" in content and not content.get("name"):
        raise pdf_content.ContentError("this content has blocks and no name, so it looks like a document -- use template 'document'")
    data = pdf_content.normalize_resume(content)
    one_page = str(args.get("fit") or "one_page").strip().lower() != "natural"
    steps = pdf_render.FIT_STEPS if one_page else pdf_render.NATURAL
    default_font = "classic" if template == "classic" else "modern"
    fallback = re.sub(r"[^A-Za-z0-9]+", "_", data["name"]).strip("_") + "_Resume"
    result, step = _render(pdf_render.RESUME_TEMPLATES[template], data, args, default_font, steps, fallback[:80])
    result["template"] = template
    if one_page:
        if result["pages"] == 1 and step != (1.0, 1.0):
            result["note"] = "Spacing/type were tightened slightly to fit one page."
        elif result["pages"] > 1:
            result["note"] = (
                f"Content needs {result['pages']} pages at a readable size, so it was left at natural size "
                "rather than shrunk further. Trim content or use fit 'natural' if that's intended."
            )
    return result


def _document(args):
    content = _content_arg(args, {"template", "style", "fit", "filename", "content"})
    if isinstance(content, dict) and "blocks" not in content and any(k in content for k in ("name", "experience", "education", "skills")):
        raise pdf_content.ContentError(
            f"this content looks like a resume -- use template {', '.join(repr(t) for t in pdf_render.RESUME_TEMPLATES)}, "
            "or give the document template a title and blocks"
        )
    data = pdf_content.normalize_document(content)
    fallback = re.sub(r"[^A-Za-z0-9]+", "_", data["title"]).strip("_")[:80] or "Document"
    result, _ = _render("document.html.j2", data, args, "modern", pdf_render.NATURAL, fallback)
    result["template"] = "document"
    return result


def _generate(args):
    template = str(args.get("template") or "").strip().lower()
    if not template:
        # No template named: infer from the content's shape rather than failing the call.
        probe = args.get("content") if isinstance(args.get("content"), dict) else args
        template = "document" if "blocks" in probe else pdf_render.DEFAULT_RESUME_TEMPLATE
    if template == "document":
        return _document(args)
    if template not in pdf_render.RESUME_TEMPLATES:
        raise pdf_content.ContentError(f"template '{template}' is not one of: {', '.join(_TEMPLATES)}")
    return _resume(args, template)


_CONTROL_KEYS = {"template", "style", "fit", "filename", "content"}


def _save_to_workspace(workspace, args, result):
    """Save the PDF and the exact arguments that produced it, so a later turn (which sees none of
    this turn's tool calls) can read the spec, change what needs changing, and regenerate."""
    stem = result["filename"][:-len(".pdf")]
    spec = {"template": result["template"], "content": _content_arg(args, _CONTROL_KEYS), "filename": stem}
    for key in ("style", "fit"):
        if args.get(key):
            spec[key] = args[key]
    with open(generated_files.resolve(result["url"].rsplit("/", 1)[-1]), "rb") as f:
        pdf = f.read()
    try:
        for rel, data in ((stem + ".pdf", pdf), (stem + ".pdf.json", (json.dumps(spec, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))):
            path = file_tools.resolve_workspace_path(workspace, rel)
            if os.path.exists(path):
                backup_tools.backup_file(path)
            with open(path, "wb") as f:
                f.write(data)
    except (OSError, ValueError) as e:
        result["workspace_save_error"] = f"The download link works, but the files could not be saved in the workspace: {e}"
        return
    result["saved_to"] = stem + ".pdf"
    result["spec"] = stem + ".pdf.json"
    result["revise"] = (
        f"To change this PDF: read_file {stem}.pdf.json, edit the content or style, and call generate_pdf again "
        f"with filename \"{stem}\". That replaces both files."
    )


def generate_pdf(args, workspace=""):
    result = _guarded(_generate, args)
    if workspace and isinstance(args, dict) and "error" not in result:
        _save_to_workspace(workspace, args, result)
    return result


def resolve_generated_file(token):
    return generated_files.resolve(token)


def display_name_for_token(token):
    return generated_files.display_name(token)

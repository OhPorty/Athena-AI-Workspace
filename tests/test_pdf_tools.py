import io
import json
import os

import pypdf
import pytest
from fastapi import HTTPException

import main
import pdf_content
import pdf_render
import generated_files
import pdf_tools
from pdf_fixtures import DOCUMENT, LONG_RESUME, RESUME


@pytest.fixture(autouse=True)
def pdf_dir(tmp_path, monkeypatch):
    """Generated PDFs go to a throwaway dir, never Athena's real generated_files/."""
    d = tmp_path / "pdfs"
    monkeypatch.setattr(generated_files, "ROOT", str(d))
    return d


def _read(result):
    path = pdf_tools.resolve_generated_file(result["url"].rsplit("/", 1)[-1])
    return pypdf.PdfReader(path)


def _text(reader):
    return "\n".join(p.extract_text() for p in reader.pages)


# ---------- content normalization (no browser) ----------

def test_resume_requires_a_name():
    with pytest.raises(pdf_content.ContentError, match="name is required"):
        pdf_content.normalize_resume({"experience": []})


def test_bullets_accept_a_newline_string_and_strip_glyphs_without_rewording():
    data = pdf_content.normalize_resume({
        "name": "A", "experience": [{"title": "T", "bullets": "• Managed the team\n- Tracked sales  \n\n2) Ran payroll"}],
    })
    assert data["experience"][0]["bullets"] == ["Managed the team", "Tracked sales", "Ran payroll"]


def test_dates_from_start_end_or_single_string():
    data = pdf_content.normalize_resume({"name": "A", "experience": [
        {"title": "T", "start": "May 2015", "end": "March 2017"},
        {"title": "U", "dates": "2019 - Present"},
        {"title": "V", "start": "2020"},
    ]})
    assert [e["dates"] for e in data["experience"]] == ["May 2015 – March 2017", "2019 - Present", "2020"]


@pytest.mark.parametrize("skills", [
    ["Excel", "Payroll"],
    "Excel, Payroll",
    {"Tools": ["Excel", "Payroll"]},
    [{"group": "Tools", "items": ["Excel", "Payroll"]}],
])
def test_skills_accept_every_common_shape(skills):
    groups = pdf_content.normalize_resume({"name": "A", "skills": skills})["skills"]
    assert [i for g in groups for i in g["items"]] == ["Excel", "Payroll"]


def test_links_drop_dangerous_schemes_and_complete_bare_domains():
    data = pdf_content.normalize_resume({"name": "A", "contact": {
        "email": "a@example.com",
        "links": ["javascript:alert(1)", "linkedin.com/in/a", {"label": "Site", "url": "data:text/html,x"}, "file:///etc/passwd"],
    }})
    assert data["email_url"] == "mailto:a@example.com"
    assert [l["url"] for l in data["links"]] == ["https://linkedin.com/in/a"]


def test_errors_name_the_exact_field():
    with pytest.raises(pdf_content.ContentError, match=r"experience\[1\] needs a title or company"):
        pdf_content.normalize_resume({"name": "A", "experience": [{"title": "T"}, {"bullets": ["x"]}]})
    with pytest.raises(pdf_content.ContentError, match=r"experience\[0\] must be an object"):
        pdf_content.normalize_resume({"name": "A", "experience": ["oops"]})


def test_document_block_aliases_and_table_padding():
    data = pdf_content.normalize_document({"title": "T", "blocks": [
        "plain string", {"type": "h2", "text": "Head"}, {"type": "list", "items": ["a", "b"]},
        {"type": "table", "headers": ["A", "B", "C"], "rows": [["1", "2"], ["3", "4", "5"]]},
    ]})
    kinds = [b["type"] for b in data["blocks"]]
    assert kinds == ["paragraph", "heading", "bullets", "table"]
    assert data["blocks"][1]["level"] == 2
    assert data["blocks"][3]["rows"] == [["1", "2", ""], ["3", "4", "5"]]


def test_document_rejects_unknown_block_type():
    with pytest.raises(pdf_content.ContentError, match="not one of"):
        pdf_content.normalize_document({"title": "T", "blocks": [{"type": "carousel"}]})


@pytest.mark.parametrize("raw,expected", [("#abc", "#aabbcc"), ("2F6BD0", "#2f6bd0")])
def test_hex_normalization(raw, expected):
    assert pdf_content.normalize_hex(raw) == expected


@pytest.mark.parametrize("accent", ["#ffe066", "#7CFC00", "#00e5ff", "#ff1493", "#222222", "#ffffff"] + list(pdf_render.PALETTES.values()))
def test_palette_derivation_stays_legible_for_any_accent(accent):
    # HSL lightness lies about brightness (pale yellow at L=0.4 is still pale), so check real contrast.
    palette = pdf_render.derive_palette(accent)
    assert pdf_render.contrast_ratio(palette["text"], "#ffffff") >= 4.5
    assert pdf_render.contrast_ratio(palette["light"], palette["dark"]) >= 4.5


# ---------- tool argument handling (no browser) ----------

def test_bad_palette_and_template_report_the_valid_options():
    out = pdf_tools.generate_pdf({"content": {"name": "A"}, "style": {"palette": "neon"}})
    assert "style.palette 'neon'" in out["error"] and "navy" in out["error"]
    out = pdf_tools.generate_pdf({"content": {"name": "A"}, "template": "fancy"})
    assert "template 'fancy'" in out["error"] and "sidebar" in out["error"]


def test_non_dict_arguments_are_an_error_not_a_crash():
    assert "error" in pdf_tools.generate_pdf("not a dict")


def test_generated_file_resolution_blocks_traversal(pdf_dir):
    pdf_dir.mkdir()
    (pdf_dir / "abc__x.pdf").write_bytes(b"%PDF-1.4")
    assert pdf_tools.resolve_generated_file("abc__x.pdf") is not None
    assert pdf_tools.resolve_generated_file("../../etc/passwd") is None
    assert pdf_tools.resolve_generated_file("missing.pdf") is None
    assert pdf_tools.display_name_for_token("abc__x.pdf") == "x.pdf"


def test_one_tool_covers_every_template():
    assert [s["function"]["name"] for s in pdf_tools.PDF_TOOL_SCHEMAS] == ["generate_pdf"]
    params = pdf_tools.PDF_TOOL_SCHEMAS[0]["function"]["parameters"]
    assert params["properties"]["template"]["enum"] == ["sidebar", "banner", "classic", "document"]
    assert params["required"] == ["template", "content"]


def test_template_is_inferred_from_content_shape_when_omitted():
    assert pdf_tools.generate_pdf({"content": DOCUMENT})["template"] == "document"
    assert pdf_tools.generate_pdf({"content": RESUME})["template"] == "sidebar"


def test_wrong_template_for_the_content_gets_a_pointed_error():
    out = pdf_tools.generate_pdf({"template": "sidebar", "content": DOCUMENT})
    assert "looks like a document" in out["error"]
    out = pdf_tools.generate_pdf({"template": "document", "content": RESUME})
    assert "looks like a resume" in out["error"]
    out = pdf_tools.generate_pdf({"template": "fancy", "content": RESUME})
    assert "document" in out["error"] and "sidebar" in out["error"]


# ---------- real rendering (Chromium) ----------

def test_every_resume_template_fits_one_letter_page_with_clickable_links():
    for template in pdf_render.RESUME_TEMPLATES:
        out = pdf_tools.generate_pdf({"template": template, "content": RESUME})
        assert "error" not in out, out
        assert out["pages"] == 1, f"{template} spilled to {out['pages']} pages"
        reader = _read(out)
        assert [(float(p.mediabox.width), float(p.mediabox.height)) for p in reader.pages] == [(612.0, 792.0)]
        annots = [a.get_object().get("/A", {}).get("/URI", "") for a in reader.pages[0].get("/Annots", [])]
        assert any(u.startswith("mailto:jordan.ellis@") for u in annots), f"{template}: email isn't a link"


def test_resume_text_is_preserved_verbatim():
    out = pdf_tools.generate_pdf({"template": "sidebar", "content": RESUME})
    text = " ".join(_text(_read(out)).split())
    for e in RESUME["experience"]:
        for bullet in e["bullets"]:
            assert " ".join(bullet.split()) in text


def test_long_resume_paginates_cleanly_instead_of_shrinking_to_nothing():
    out = pdf_tools.generate_pdf({"template": "banner", "content": LONG_RESUME})
    assert out["pages"] > 1
    assert "left at natural size" in out["note"]
    reader = _read(out)
    sizes = {(float(p.mediabox.width), float(p.mediabox.height)) for p in reader.pages}
    assert sizes == {(612.0, 792.0)}


def test_classic_template_headings_extract_as_whole_words_for_ats_parsers():
    out = pdf_tools.generate_pdf({"template": "classic", "content": RESUME})
    text = _text(_read(out))
    for heading in ("SUMMARY", "EXPERIENCE", "SKILLS", "EDUCATION", "CERTIFICATIONS"):
        assert heading in text, f"{heading} was split by letter-spacing"


def test_fit_natural_never_tightens():
    out = pdf_tools.generate_pdf({"template": "classic", "content": RESUME, "fit": "natural"})
    assert out["pages"] == 2 and "note" not in out


def test_a4_page_size():
    out = pdf_tools.generate_pdf({"content": RESUME, "style": {"page_size": "a4"}})
    page = _read(out).pages[0]
    assert round(float(page.mediabox.width)) == 595


def test_content_is_escaped_not_interpreted():
    nasty = {**RESUME, "name": '<script>window.x=1</script> "Q" & \\ Co', "summary": "</style><b>bold</b>"}
    out = pdf_tools.generate_pdf({"template": "classic", "content": nasty})
    assert "error" not in out, out
    text = _text(_read(out))
    assert "<script>window.x=1</script>" in text and "</style><b>bold</b>" in text


def test_flattened_arguments_still_work_and_filename_is_sanitized():
    out = pdf_tools.generate_pdf({**RESUME, "filename": "../../My Résumé!.pdf"})
    assert "error" not in out, out
    assert out["filename"] == "My_Rsum.pdf"
    assert os.path.basename(out["url"]).endswith("__My_Rsum.pdf")


def test_document_renders_with_page_numbers_and_aligned_numbers():
    out = pdf_tools.generate_pdf({"template": "document", "content": DOCUMENT})
    assert "error" not in out, out
    text = _text(_read(out))
    assert "Quarterly Operations Review" in text and "$412,300" in text and "1 / 1" in text


def test_multi_page_document_repeats_table_header_and_numbers_pages():
    rows = [[f"Row {i}", f"${i * 100:,}"] for i in range(120)]
    content = {"title": "Big", "blocks": [{"type": "table", "headers": ["Item", "Cost"], "rows": rows}]}
    out = pdf_tools.generate_pdf({"template": "document", "content": content})
    reader = _read(out)
    assert len(reader.pages) >= 2
    # letter-spaced uppercase labels extract with gaps ("I TE M"), so compare without spaces
    assert all("ITEM" in p.extract_text().replace(" ", "").upper() for p in reader.pages)
    assert f"{len(reader.pages)} / {len(reader.pages)}" in reader.pages[-1].extract_text()


def test_dispatch_and_download_route_round_trip():
    ctx = main.ToolContext(session_id="s1", workspace="", search_url="", bot=None, session_key=None,
                           req=None, plan_state=None, passthrough_session_id=None, require_athena_bots_session=False)
    out = main.dispatch_tool("generate_pdf", {"template": "document", "content": DOCUMENT}, ctx)
    assert "error" not in out, out
    response = main.get_generated_file(out["url"].rsplit("/", 1)[-1])
    assert response.media_type == "application/pdf" and response.filename == out["filename"]
    with pytest.raises(HTTPException) as exc:
        main.get_generated_file("nope.pdf")
    assert exc.value.status_code == 404


# ---------- saved into the workspace so a later turn can revise ----------

def test_with_a_workspace_the_pdf_and_its_spec_are_saved_there(tmp_path):
    out = pdf_tools.generate_pdf({"template": "classic", "content": RESUME, "style": {"palette": "forest"}, "filename": "Ada"}, str(tmp_path))
    assert out["saved_to"] == "Ada.pdf" and out["spec"] == "Ada.pdf.json" and "Ada.pdf.json" in out["revise"]
    assert (tmp_path / "Ada.pdf").read_bytes() == open(pdf_tools.resolve_generated_file(out["url"].rsplit("/", 1)[-1]), "rb").read()
    spec = json.loads((tmp_path / "Ada.pdf.json").read_text())
    assert spec == {"template": "classic", "content": RESUME, "filename": "Ada", "style": {"palette": "forest"}}


def test_the_saved_spec_regenerates_the_same_pdf_when_fed_back(tmp_path):
    first = pdf_tools.generate_pdf({"template": "sidebar", "content": RESUME, "filename": "Ada"}, str(tmp_path))
    spec = json.loads((tmp_path / "Ada.pdf.json").read_text())
    spec["content"]["headline"] = "Principal Engineer"
    second = pdf_tools.generate_pdf(spec, str(tmp_path))
    assert second["saved_to"] == "Ada.pdf" and "error" not in second
    saved_text = "".join(p.extract_text() for p in pypdf.PdfReader(str(tmp_path / "Ada.pdf")).pages)
    assert "PRINCIPALENGINEER" in saved_text.replace(" ", "").upper()   # the sidebar template letter-spaces its headline
    assert json.loads((tmp_path / "Ada.pdf.json").read_text())["content"]["headline"] == "Principal Engineer"
    assert first["pages"] == second["pages"]


def test_revising_keeps_a_restorable_backup_of_the_previous_version(tmp_path, monkeypatch):
    import backup_tools
    monkeypatch.setattr(backup_tools, "_BACKUP_ROOT", str(tmp_path / "backups"))
    ws = tmp_path / "ws"
    ws.mkdir()
    pdf_tools.generate_pdf({"template": "classic", "content": RESUME, "filename": "Ada"}, str(ws))
    old = (ws / "Ada.pdf.json").read_text()
    changed = json.loads(old)
    changed["content"]["name"] = "Someone Else"
    pdf_tools.generate_pdf(changed, str(ws))
    assert backup_tools._read_backup_manifest() and (ws / "Ada.pdf.json").read_text() != old


def test_a_flattened_call_is_saved_in_the_documented_shape(tmp_path):
    out = pdf_tools.generate_pdf({**RESUME, "filename": "Flat"}, str(tmp_path))
    spec = json.loads((tmp_path / "Flat.pdf.json").read_text())
    assert out["spec"] == "Flat.pdf.json" and spec["content"]["name"] == RESUME["name"] and "content" in spec


def test_no_workspace_means_no_files_and_a_failed_call_saves_nothing(tmp_path):
    assert "saved_to" not in pdf_tools.generate_pdf({"content": RESUME})
    ws = tmp_path / "ws"
    ws.mkdir()
    bad = pdf_tools.generate_pdf({"template": "fancy", "content": RESUME}, str(ws))
    assert "error" in bad and list(ws.iterdir()) == []


def test_an_unwritable_workspace_still_returns_the_download_link(tmp_path):
    out = pdf_tools.generate_pdf({"content": RESUME}, str(tmp_path / "missing" / "dir"))
    assert "error" not in out and out["url"] and "workspace_save_error" in out and "saved_to" not in out


def test_dispatch_passes_the_turns_workspace_through(tmp_path):
    ctx = main.ToolContext(session_id="s1", workspace=str(tmp_path), search_url="", bot=None, session_key=None,
                           req=None, plan_state=None, passthrough_session_id=None, require_athena_bots_session=False)
    out = main.dispatch_tool("generate_pdf", {"template": "document", "content": DOCUMENT, "filename": "Memo"}, ctx)
    assert out["saved_to"] == "Memo.pdf" and (tmp_path / "Memo.pdf").is_file() and (tmp_path / "Memo.pdf.json").is_file()

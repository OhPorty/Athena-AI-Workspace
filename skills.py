import os
import re

import yaml
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")


def _parse_skill_frontmatter(content: str):
    """Splits a SKILL.md's YAML frontmatter from its Markdown body.
    Matches the open Agent Skills / SKILL.md standard, so skills
    built here are usable elsewhere too and vice versa. Returns
    (metadata, body), or (None, content) if there's no valid
    frontmatter block."""
    if not content.startswith("---"):
        return None, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, content
    try:
        metadata = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None, content
    return metadata, parts[2].lstrip(chr(10))


def scan_skills():
    """Lists every skill: a folder under SKILLS_DIR with a SKILL.md
    that has at least a name and description in its frontmatter.
    Only name+description are read here (progressive disclosure
    level 1) -- cheap enough to include for every skill on every
    request."""
    skills = []
    if not os.path.isdir(SKILLS_DIR):
        return skills
    for entry in sorted(os.listdir(SKILLS_DIR)):
        skill_md = os.path.join(SKILLS_DIR, entry, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        try:
            content = open(skill_md, encoding="utf-8").read()
        except Exception:
            continue
        metadata, _ = _parse_skill_frontmatter(content)
        if not metadata or not metadata.get("name") or not metadata.get("description"):
            continue
        skills.append({"name": metadata["name"], "description": metadata["description"], "folder": entry})
    return skills


def load_skill(name: str):
    """Returns one skill's full body -- progressive disclosure level
    2, called by the model via the load_skill tool once it decides a
    skill is relevant based on its description."""
    for s in scan_skills():
        if s["name"] == name:
            content = open(os.path.join(SKILLS_DIR, s["folder"], "SKILL.md"), encoding="utf-8").read()
            _, body = _parse_skill_frontmatter(content)
            return {"name": name, "content": body}
    return {"error": "No skill named '" + name + "' found."}


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "skill"


class SkillParseIn(BaseModel):
    content: str


@router.post("/api/skills/parse")
def parse_skill_upload(req: SkillParseIn):
    """Parses an uploaded SKILL.md's frontmatter+body without saving
    anything, so the person can review/edit in the form before it
    actually gets written to disk."""
    metadata, body = _parse_skill_frontmatter(req.content)
    if not metadata or not metadata.get("name") or not metadata.get("description"):
        return {"error": "This file doesn't have valid SKILL.md frontmatter (needs at least name and description)."}
    return {"name": metadata.get("name"), "description": metadata.get("description"), "body": body}


@router.get("/api/skills")
def list_skills():
    return {"skills": scan_skills()}


@router.get("/api/skills/{folder}")
def get_skill(folder: str):
    if ".." in folder or "/" in folder:
        return {"error": "Invalid folder name."}
    skill_md = os.path.join(SKILLS_DIR, folder, "SKILL.md")
    if not os.path.isfile(skill_md):
        return {"error": "Skill not found."}
    content = open(skill_md, encoding="utf-8").read()
    metadata, body = _parse_skill_frontmatter(content)
    if not metadata:
        return {"error": "Skill file has no valid frontmatter."}
    return {"name": metadata.get("name"), "description": metadata.get("description"), "body": body, "folder": folder}


class SkillIn(BaseModel):
    name: str
    description: str
    body: str
    folder: Optional[str] = None


@router.post("/api/skills")
def save_skill(req: SkillIn):
    os.makedirs(SKILLS_DIR, exist_ok=True)
    folder = req.folder or _slugify(req.name)
    if ".." in folder or "/" in folder:
        return {"error": "Invalid folder name."}
    skill_dir = os.path.join(SKILLS_DIR, folder)
    os.makedirs(skill_dir, exist_ok=True)
    frontmatter = yaml.safe_dump({"name": req.name, "description": req.description}, default_flow_style=False, sort_keys=False)
    content = "---" + chr(10) + frontmatter + "---" + chr(10) + chr(10) + req.body
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(content)
    return {"ok": True, "folder": folder}


@router.delete("/api/skills/{folder}")
def delete_skill(folder: str):
    if ".." in folder or "/" in folder:
        return {"error": "Invalid folder name."}
    skill_dir = os.path.join(SKILLS_DIR, folder)
    if os.path.isdir(skill_dir):
        import shutil
        shutil.rmtree(skill_dir)
        return {"ok": True}
    return {"error": "Skill not found."}


SKILL_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Loads the full instructions for one available skill by name. Call this when a skill's description (listed in your system context) matches what you're being asked to do.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The exact skill name, as listed in your available skills."},
                },
                "required": ["name"],
            },
        },
    },
]


def get_skills_context(allowed_skills=None) -> str:
    skills = scan_skills()
    if allowed_skills is not None:
        allowed_set = set(allowed_skills)
        skills = [s for s in skills if s["name"] in allowed_set]
    if not skills:
        return ""
    lines = ["Available skills: call load_skill(name) to load one's full instructions when its description matches the current task."]
    for s in skills:
        lines.append("- " + s["name"] + ": " + s["description"])
    return chr(10) + chr(10) + chr(10).join(lines)

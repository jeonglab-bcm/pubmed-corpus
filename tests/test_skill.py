"""The skill file is part of the product: check it says only what the tool can do."""

import re
from pathlib import Path

import yaml

from pubmed_corpus import cli

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / ".claude" / "skills" / "disease-corpus" / "SKILL.md"


def frontmatter_and_body():
    text = SKILL.read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert m, "SKILL.md must start with YAML frontmatter"
    return yaml.safe_load(m.group(1)), m.group(2)


def test_frontmatter():
    meta, _ = frontmatter_and_body()
    assert re.fullmatch(r"[a-z0-9-]{1,64}", meta["name"])
    assert meta["name"] == SKILL.parent.name
    assert 50 < len(meta["description"]) <= 1024


def test_every_command_in_the_skill_exists_and_every_command_is_documented():
    _, body = frontmatter_and_body()
    used = set(re.findall(r"pubmed-corpus ([a-z][a-z-]*)", body))
    assert used <= set(cli.COMMANDS), f"unknown commands in SKILL.md: {used - set(cli.COMMANDS)}"
    assert set(cli.COMMANDS) <= used, f"commands missing from SKILL.md: {set(cli.COMMANDS) - used}"


def test_documented_command_lines_parse():
    _, body = frontmatter_and_body()
    for line in re.findall(r"`(pubmed-corpus [^`]+)`", body):
        argv = line.split()[1:]
        argv = [a.replace("<slug>", "x").replace("<UI>", "D1").replace("<PMID>:<key>", "1:k").replace('"<disease>"', "d")
                for a in argv]
        if any("<" in a for a in argv):
            continue
        cli.parser().parse_args(argv)


def test_files_the_skill_names_are_the_files_the_tool_writes():
    _, body = frontmatter_and_body()
    named = set(re.findall(r"`out/([a-z_]+\.(?:tsv|json))`", body))
    src = "".join(p.read_text() for p in (ROOT / "src" / "pubmed_corpus").glob("*.py"))
    for name in named:
        assert f'"{name}"' in src, f"SKILL.md names out/{name} but no step writes it"

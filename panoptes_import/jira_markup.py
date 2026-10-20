"""Jira wiki markup -> Markdown (the common subset: headings, formatting, lists, code, links, tables, quotes)."""
import re

_CODE = re.compile(r"\{(code|noformat)(?::([\w+-]+))?[^}]*\}(.*?)\{\1\}", re.S)


def jira_to_md(text: str) -> str:
    if not text:
        return ""
    blocks: list[str] = []

    def stash(m):
        lang = m.group(2) or ""
        blocks.append(f"```{lang}\n{m.group(3).strip(chr(10))}\n```")
        return f"\x00{len(blocks) - 1}\x00"

    def stash_inline(m):
        blocks.append(f"`{m.group(1)}`")
        return f"\x00{len(blocks) - 1}\x00"

    text = re.sub(r"\{\{(.+?)\}\}", stash_inline, text.replace("\r\n", "\n"))
    text = _CODE.sub(stash, text)
    text = re.sub(r"\{quote\}(.*?)\{quote\}", lambda m: "\n".join("> " + l for l in m.group(1).strip().splitlines()), text, flags=re.S)
    out, table = [], False
    for line in text.split("\n"):
        line = re.sub(r"^h([1-6])\.\s+", lambda m: "\x01" * int(m.group(1)) + " ", line)
        line = re.sub(r"^bq\.\s+", "> ", line)
        m = re.match(r"^([*#-]+)\s+(.*)", line)
        if m and not re.match(r"^\*[^\s*].*\*$", line):
            marks, rest = m.groups()
            indent = "  " * (len(marks) - 1)
            line = f"{indent}{'1.' if marks[-1] == '#' else '-'} {rest}"
        if line.startswith("||"):
            cells = [c.strip() for c in line.strip("|").split("||")]
            line = "| " + " | ".join(cells) + " |\n|" + "\x02\x02\x02|" * len(cells)
            table = True
        elif line.startswith("|") and table:
            line = "| " + " | ".join(c.strip() for c in line.strip("|").split("|")) + " |"
        else:
            if table:
                out.append("")
            table = False
        out.append(line)
    text = "\n".join(out)
    text = re.sub(r"\[([^\]|]+)\|(https?://[^\]]+)\]", r"[\1](\2)", text)
    text = re.sub(r"\[(https?://[^\]|]+)\]", r"<\1>", text)
    text = re.sub(r"(?<![\w*])\*(\S(?:[^*\n]*?\S)?)\*(?![\w*])", r"**\1**", text)
    text = re.sub(r"(?<![\w_])_(\S(?:[^_\n]*?\S)?)_(?![\w_])", r"*\1*", text)
    text = re.sub(r"(?<![\w-])-(\S(?:[^-\n]*?\S)?)-(?![\w-])", r"~~\1~~", text)
    text = re.sub(r"!([^!\s|]+)(?:\|[^!]*)?!", r"*(image: \1)*", text)
    text = re.sub(r"\[~([^\]]+)\]", r"@\1", text)
    text = text.replace("\x01", "#").replace("\x02", "-")
    for i, b in enumerate(blocks):
        text = text.replace(f"\x00{i}\x00", b)
    return text.strip()

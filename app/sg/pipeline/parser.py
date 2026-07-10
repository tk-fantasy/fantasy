"""Markdown 文档解析 — 从 kg-pipeline 移植，零改造（纯算法，无外部依赖）。

解析 docs 目录下的 .md 文件，按 ## 标题切分段落，提取标题/链接/分类。
parse_all 接收 docs_root 字符串而非 Config 对象。
"""
import json
import re
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Section:
    heading: str
    content: str
    entities: list[str] = field(default_factory=list)


@dataclass
class Document:
    id: str
    title: str
    category: str
    subcategory: Optional[str]
    filepath: str
    sections: list[Section] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    raw_text: str = ""


def load_index(index_path: str) -> tuple[list[dict], dict]:
    with open(index_path, "r", encoding="utf-8") as f:
        entries = json.load(f)
    cat_map: dict[str, list[dict]] = {}
    for entry in entries:
        cat_map.setdefault(entry["category"], []).append(entry)
    return entries, cat_map


def extract_title(markdown_text: str) -> str:
    match = re.search(r"^#\s+(.+)$", markdown_text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def extract_links(markdown_text: str) -> list[str]:
    links = []
    for m in re.finditer(r"\(([^)]+\.md)\)", markdown_text):
        candidate = m.group(1)
        if not candidate.startswith("http"):
            links.append(candidate)
    return links


def split_sections(markdown_text: str) -> list[Section]:
    lines = markdown_text.split("\n")
    sections: list[Section] = []
    current_heading = "前言"
    current_content: list[str] = []

    for line in lines:
        m = re.match(r"^(#{2,4})\s+(.+)$", line)
        if m:
            if current_content:
                sections.append(Section(
                    heading=current_heading,
                    content="\n".join(current_content).strip(),
                ))
            current_heading = m.group(2).strip()
            current_content = []
        else:
            current_content.append(line)

    if current_content:
        sections.append(Section(
            heading=current_heading,
            content="\n".join(current_content).strip(),
        ))

    return sections


def find_markdown_files(root: str) -> list[str]:
    files = []
    for dirpath, _, filenames in os.walk(root):
        for f in filenames:
            if f.endswith(".md"):
                files.append(os.path.join(dirpath, f))
    return files


def parse_all(docs_root: str, index_path: str = "") -> tuple[list[Document], dict[str, list[str]]]:
    """解析 docs_root 下的所有 .md 文件。

    Args:
        docs_root: 文档根目录路径
        index_path: 可选的索引文件路径（含 category/subcategory 映射）
    Returns:
        (文档列表, 实体-文档映射)
    """
    index_entries: list[dict] = []
    if index_path and os.path.isfile(index_path):
        index_entries, _ = load_index(index_path)

    all_docs: list[Document] = []
    entity_doc_map: dict[str, list[str]] = {}

    if not os.path.isdir(docs_root):
        return all_docs, entity_doc_map

    # 自动发现 docs_root 下的所有一级子目录作为分类
    try:
        subdirs = [d for d in os.listdir(docs_root)
                   if os.path.isdir(os.path.join(docs_root, d))]
    except Exception:
        subdirs = []

    category_dir_map = {}
    for d in sorted(subdirs):
        if d.startswith(".") or d.startswith("_"):
            continue
        category_dir_map[d] = d

    # 如果 docs_root 本身有 .md 文件，加一个根目录分类
    root_md = [f for f in os.listdir(docs_root)
               if f.endswith(".md") and os.path.isfile(os.path.join(docs_root, f))]
    if root_md:
        category_dir_map["."] = "根目录"

    # 从 index 构建文档级别的 subcategory 映射
    doc_sub_map: dict[str, str] = {}
    for entry in index_entries:
        filepath = entry.get("filepath", "")
        filename = os.path.basename(filepath)
        doc_id = os.path.splitext(filename)[0]
        sub = entry.get("subcategory", "")
        if sub:
            doc_sub_map[doc_id] = sub

    for category, dirname in category_dir_map.items():
        if dirname == ".":
            md_files = [os.path.join(docs_root, f)
                        for f in os.listdir(docs_root)
                        if f.endswith(".md") and os.path.isfile(os.path.join(docs_root, f))]
        else:
            dirpath = os.path.join(docs_root, dirname)
            if not os.path.isdir(dirpath):
                continue
            md_files = find_markdown_files(dirpath)
        for filepath in md_files:
            with open(filepath, "r", encoding="utf-8") as f:
                raw = f.read()

            filename = os.path.basename(filepath)
            doc_id = os.path.splitext(filename)[0]
            title = extract_title(raw)
            sections = split_sections(raw)
            links = extract_links(raw)

            subcategory = doc_sub_map.get(doc_id)

            doc = Document(
                id=doc_id,
                title=title or doc_id,
                category=category,
                subcategory=subcategory,
                filepath=os.path.relpath(filepath, docs_root),
                sections=sections,
                links=links,
                raw_text=raw,
            )
            all_docs.append(doc)

    return all_docs, entity_doc_map

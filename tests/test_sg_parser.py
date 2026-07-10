"""Tests for app/sg/pipeline/parser.py — Markdown 文档解析。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.sg.pipeline.parser import (
    Document,
    Section,
    extract_links,
    extract_title,
    find_markdown_files,
    parse_all,
    split_sections,
)


class TestExtractTitle:
    def test_h1_title(self):
        assert extract_title("# Docker 部署指南\n正文") == "Docker 部署指南"

    def test_no_h1(self):
        assert extract_title("正文，无一级标题") == ""

    def test_h2_not_matched(self):
        # 仅匹配单个 # 的一级标题，## 不算
        assert extract_title("## 二级标题\n正文") == ""


class TestExtractLinks:
    def test_md_links(self):
        text = "参见 [安装](install.md) 和 [配置](config.md)"
        assert extract_links(text) == ["install.md", "config.md"]

    def test_skip_http_links(self):
        text = "外部链接 [官网](https://example.com/doc.md)"
        assert extract_links(text) == []

    def test_no_links(self):
        assert extract_links("纯文本无链接") == []


class TestSplitSections:
    def test_split_by_h2(self):
        text = "# 标题\n前言内容\n## 第一节\n内容1\n## 第二节\n内容2"
        sections = split_sections(text)
        assert len(sections) == 3
        assert sections[0].heading == "前言"
        assert sections[1].heading == "第一节"
        assert sections[2].heading == "第二节"
        assert "内容1" in sections[1].content

    def test_no_sections(self):
        sections = split_sections("纯文本无小标题")
        assert len(sections) == 1
        assert sections[0].heading == "前言"


class TestFindMarkdownFiles:
    def test_finds_md_files(self, tmp_path):
        (tmp_path / "a.md").write_text("# A", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.md").write_text("# B", encoding="utf-8")
        (tmp_path / "c.txt").write_text("not md", encoding="utf-8")

        files = find_markdown_files(str(tmp_path))
        md_names = sorted(Path(f).name for f in files)
        assert md_names == ["a.md", "b.md"]


class TestParseAll:
    def test_empty_dir(self, tmp_path):
        docs, entity_map = parse_all(str(tmp_path), "")
        assert docs == []
        assert entity_map == {}

    def test_nonexistent_dir(self):
        docs, entity_map = parse_all("/nonexistent/path", "")
        assert docs == []
        assert entity_map == {}

    def test_parse_single_doc(self, tmp_path):
        cat_dir = tmp_path / "01-安装"
        cat_dir.mkdir()
        (cat_dir / "Docker部署.md").write_text(
            "# Docker 部署\n## 安装\n安装步骤\n## 配置\n配置参数",
            encoding="utf-8",
        )

        docs, entity_map = parse_all(str(tmp_path), "")
        assert len(docs) == 1
        doc = docs[0]
        assert isinstance(doc, Document)
        assert doc.title == "Docker 部署"
        assert doc.category == "01-安装"
        assert len(doc.sections) == 3  # 前言 + 安装 + 配置
        assert doc.sections[1].heading == "安装"
        assert "安装步骤" in doc.sections[1].content

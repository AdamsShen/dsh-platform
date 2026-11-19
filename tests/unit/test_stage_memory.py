"""跨阶段记忆只有文件这一条通道：摘要追加、滚动读取、开场注入。"""

from __future__ import annotations

from pathlib import Path

from ao import memory


def test_summary_is_appended_per_stage(tmp_path: Path) -> None:
    memory.append_stage_summary(tmp_path, "draft", "决定用周报模板；风险：字段缺失")
    memory.append_stage_summary(tmp_path, "review", "结论：可以发布")

    text = memory.memory_path(tmp_path).read_text(encoding="utf-8")
    assert "## 阶段 draft" in text
    assert "## 阶段 review" in text
    assert text.index("阶段 draft") < text.index("阶段 review")


def test_empty_summary_is_ignored(tmp_path: Path) -> None:
    memory.append_stage_summary(tmp_path, "draft", "   ")
    assert not memory.memory_path(tmp_path).exists()


def test_load_memory_keeps_whole_sections(tmp_path: Path) -> None:
    """超长时只丢整段，不能把某段摘要切成半截。"""
    for index in range(30):
        memory.append_stage_summary(tmp_path, f"n{index}", "x" * 200)

    text = memory.load_memory(tmp_path, limit=800)
    assert text.startswith("## 阶段")
    assert len(text) <= 1200  # 尾部 limit + 最后一个完整段落
    assert "x" * 200 in text


def test_artifact_index_lists_newest_first(tmp_path: Path) -> None:
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "artifacts" / "b.txt").write_text("b", encoding="utf-8")

    names = memory.artifact_index(tmp_path)
    assert set(names) == {"artifacts/a.txt", "artifacts/b.txt"}


def test_bootstrap_empty_for_first_stage(tmp_path: Path) -> None:
    assert memory.bootstrap_block(tmp_path, "first", {}) == ""


def test_bootstrap_carries_memory_upstream_and_files(tmp_path: Path) -> None:
    memory.append_stage_summary(tmp_path, "collect", "拿到 12 条数据")
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "raw.json").write_text("{}", encoding="utf-8")

    block = memory.bootstrap_block(tmp_path, "draft", {"collect": {"count": 12}})
    assert "前序阶段记忆" in block
    assert "拿到 12 条数据" in block
    assert "collect: {\"count\": 12}" in block
    assert "artifacts/raw.json" in block
    # 自己的产物不当作上游
    assert "- draft:" not in block


def test_summary_prompt_truncates_large_payload() -> None:
    prompt = memory.summary_prompt("draft", {"text": "y" * 6000})
    assert "已截断" in prompt
    assert len(prompt) < 5000

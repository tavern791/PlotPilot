"""FileChapterRepository 集成测试"""
import pytest
import tempfile
import shutil
from pathlib import Path
from domain.novel.entities.chapter import Chapter
from domain.novel.value_objects.novel_id import NovelId
from domain.novel.value_objects.chapter_id import ChapterId
from infrastructure.persistence.storage.file_storage import FileStorage
from infrastructure.persistence.repositories.file_chapter_repository import FileChapterRepository


class TestFileChapterRepository:
    """FileChapterRepository 集成测试"""

    @pytest.fixture
    def temp_dir(self):
        """创建临时目录"""
        temp_path = tempfile.mkdtemp()
        yield Path(temp_path)
        shutil.rmtree(temp_path)

    @pytest.fixture
    def storage(self, temp_dir):
        """创建 FileStorage 实例"""
        return FileStorage(temp_dir)

    @pytest.fixture
    def repository(self, storage):
        """创建 FileChapterRepository 实例"""
        return FileChapterRepository(storage)

    def test_save_and_get(self, repository):
        """测试保存和获取章节"""
        chapter = Chapter(
            id="chapter-1",
            novel_id=NovelId("novel-1"),
            number=1,
            title="第一章",
            content="这是第一章的内容"
        )

        repository.save(chapter)
        retrieved = repository.get_by_id(ChapterId("chapter-1"))

        assert retrieved is not None
        assert retrieved.id == "chapter-1"
        assert retrieved.novel_id.value == "novel-1"
        assert retrieved.number == 1
        assert retrieved.title == "第一章"
        assert retrieved.content == "这是第一章的内容"

    def test_get_nonexistent(self, repository):
        """测试获取不存在的章节"""
        result = repository.get_by_id(ChapterId("nonexistent"))
        assert result is None

    def test_list_by_novel(self, repository):
        """测试按小说查询章节"""
        novel_id = NovelId("novel-1")

        chapter1 = Chapter(
            id="chapter-1",
            novel_id=novel_id,
            number=1,
            title="第一章",
            content="内容1"
        )
        chapter2 = Chapter(
            id="chapter-2",
            novel_id=novel_id,
            number=2,
            title="第二章",
            content="内容2"
        )
        chapter3 = Chapter(
            id="chapter-3",
            novel_id=NovelId("novel-2"),
            number=1,
            title="另一本小说的章节",
            content="内容3"
        )

        repository.save(chapter1)
        repository.save(chapter2)
        repository.save(chapter3)

        chapters = repository.list_by_novel(novel_id)
        assert len(chapters) == 2
        chapter_ids = [c.id for c in chapters]
        assert "chapter-1" in chapter_ids
        assert "chapter-2" in chapter_ids
        assert "chapter-3" not in chapter_ids

    def test_list_by_novel_sorted(self, repository):
        """测试按小说查询章节时按序号排序"""
        novel_id = NovelId("novel-1")

        # 故意以乱序保存
        chapter3 = Chapter(
            id="chapter-3",
            novel_id=novel_id,
            number=3,
            title="第三章",
            content="内容3"
        )
        chapter1 = Chapter(
            id="chapter-1",
            novel_id=novel_id,
            number=1,
            title="第一章",
            content="内容1"
        )
        chapter2 = Chapter(
            id="chapter-2",
            novel_id=novel_id,
            number=2,
            title="第二章",
            content="内容2"
        )

        repository.save(chapter3)
        repository.save(chapter1)
        repository.save(chapter2)

        chapters = repository.list_by_novel(novel_id)
        assert len(chapters) == 3
        assert chapters[0].number == 1
        assert chapters[1].number == 2
        assert chapters[2].number == 3

    def test_delete(self, repository):
        """测试删除章节"""
        chapter = Chapter(
            id="chapter-1",
            novel_id=NovelId("novel-1"),
            number=1,
            title="第一章",
            content="内容"
        )

        repository.save(chapter)
        assert repository.exists(ChapterId("chapter-1"))

        repository.delete(ChapterId("chapter-1"))
        assert not repository.exists(ChapterId("chapter-1"))

    def test_exists(self, repository):
        """测试检查章节是否存在"""
        chapter_id = ChapterId("chapter-1")

        assert not repository.exists(chapter_id)

        chapter = Chapter(
            id="chapter-1",
            novel_id=NovelId("novel-1"),
            number=1,
            title="第一章",
            content="内容"
        )
        repository.save(chapter)

        assert repository.exists(chapter_id)

    def test_save_with_nested_path(self, repository, storage):
        """测试保存章节时创建嵌套路径"""
        chapter = Chapter(
            id="chapter-1",
            novel_id=NovelId("novel-1"),
            number=1,
            title="第一章",
            content="内容"
        )

        repository.save(chapter)

        # 验证文件存在于正确的嵌套路径
        expected_path = "novels/novel-1/chapters/chapter-1.json"
        assert storage.exists(expected_path)

    def test_list_by_novel_empty(self, repository):
        """测试查询不存在的小说的章节"""
        chapters = repository.list_by_novel(NovelId("nonexistent-novel"))
        assert len(chapters) == 0

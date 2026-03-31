"""基于文件的 Chapter 仓储实现"""
import logging
from typing import Optional, List
from domain.novel.entities.chapter import Chapter
from domain.novel.value_objects.novel_id import NovelId
from domain.novel.value_objects.chapter_id import ChapterId
from domain.novel.repositories.chapter_repository import ChapterRepository
from infrastructure.persistence.storage.backend import StorageBackend
from infrastructure.persistence.mappers.chapter_mapper import ChapterMapper

logger = logging.getLogger(__name__)


class FileChapterRepository(ChapterRepository):
    """基于文件系统的 Chapter 仓储实现

    使用 JSON 文件存储章节数据。
    章节存储在 novels/{novel_id}/chapters/{chapter_id}.json 路径下。
    """

    def __init__(self, storage: StorageBackend):
        """初始化仓储

        Args:
            storage: 存储后端
        """
        self.storage = storage

    def _get_path(self, novel_id: NovelId, chapter_id: ChapterId) -> str:
        """获取章节文件路径

        Args:
            novel_id: 小说 ID
            chapter_id: 章节 ID

        Returns:
            文件路径
        """
        return f"novels/{novel_id.value}/chapters/{chapter_id.value}.json"

    def save(self, chapter: Chapter) -> None:
        """保存章节

        Args:
            chapter: 章节实体
        """
        path = self._get_path(chapter.novel_id, ChapterId(chapter.id))
        data = ChapterMapper.to_dict(chapter)
        self.storage.write_json(path, data)

    def get_by_id(self, chapter_id: ChapterId) -> Optional[Chapter]:
        """根据 ID 获取章节

        由于章节存储在小说目录下，需要遍历所有小说目录查找。

        Args:
            chapter_id: 章节 ID

        Returns:
            章节实体，如果不存在则返回 None
        """
        # 遍历所有小说目录查找章节
        pattern = f"novels/*/chapters/{chapter_id.value}.json"
        files = self.storage.list_files(pattern)

        if not files:
            return None

        # 读取第一个匹配的文件
        data = self.storage.read_json(files[0])
        return ChapterMapper.from_dict(data)

    def list_by_novel(self, novel_id: NovelId) -> List[Chapter]:
        """列出小说的所有章节

        返回的章节列表按章节序号升序排序。

        Args:
            novel_id: 小说 ID

        Returns:
            章节列表
        """
        pattern = f"novels/{novel_id.value}/chapters/*.json"
        files = self.storage.list_files(pattern)
        chapters = []

        for file_path in files:
            try:
                data = self.storage.read_json(file_path)
                chapter = ChapterMapper.from_dict(data)
                chapters.append(chapter)
            except Exception as e:
                # 跳过损坏的文件，记录警告
                logger.warning(f"Failed to load chapter from {file_path}: {str(e)}")
                continue

        # 按章节序号排序
        chapters.sort(key=lambda c: c.number)
        return chapters

    def delete(self, chapter_id: ChapterId) -> None:
        """删除章节

        如果章节不存在，此操作不会引发错误。

        Args:
            chapter_id: 章节 ID
        """
        # 查找章节文件
        pattern = f"novels/*/chapters/{chapter_id.value}.json"
        files = self.storage.list_files(pattern)

        # 删除所有匹配的文件
        for file_path in files:
            self.storage.delete(file_path)

    def exists(self, chapter_id: ChapterId) -> bool:
        """检查章节是否存在

        Args:
            chapter_id: 章节 ID

        Returns:
            章节是否存在
        """
        pattern = f"novels/*/chapters/{chapter_id.value}.json"
        files = self.storage.list_files(pattern)
        return len(files) > 0

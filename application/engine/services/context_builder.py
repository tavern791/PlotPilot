import logging
from typing import List, Optional, TYPE_CHECKING, Dict, Any
from dataclasses import dataclass
from application.world.services.bible_service import BibleService
from domain.bible.services.relationship_engine import RelationshipEngine
from domain.novel.services.storyline_manager import StorylineManager
from domain.novel.repositories.novel_repository import NovelRepository
from domain.novel.repositories.chapter_repository import ChapterRepository
from domain.novel.repositories.plot_arc_repository import PlotArcRepository
from domain.novel.value_objects.novel_id import NovelId
from domain.ai.services.vector_store import VectorStore
from domain.ai.services.embedding_service import EmbeddingService
from domain.novel.repositories.foreshadowing_repository import ForeshadowingRepository
from application.ai.vector_retrieval_facade import VectorRetrievalFacade

if TYPE_CHECKING:
    from application.engine.dtos.scene_director_dto import SceneDirectorAnalysis

logger = logging.getLogger(__name__)


@dataclass
class Beat:
    """微观节拍（Beat）

    将章节大纲拆分为多个微观节拍，强制 AI 放慢节奏，增加感官细节。

    示例：
    - 原大纲："林羽发现真相，和苏晴争吵"
    - 拆分为 4 个节拍：
      1. 房间压抑的灯光和两人沉重的呼吸（500字）
      2. 林羽砸碎水杯，质问苏晴（800字）
      3. 苏晴不敢直视的微表情（600字）
      4. 林羽摔门而出，绝不和好（400字）
    """
    description: str  # 节拍描述
    target_words: int  # 目标字数
    focus: str  # 聚焦点：sensory（感官）、dialogue（对话）、action（动作）、emotion（情绪）


class ContextBuilder:
    """上下文构建器应用服务

    智能组装章节生成所需的上下文，控制在 35K token 预算内。

    上下文分层：
    - Layer 1: 核心上下文 (~5K tokens) - 小说元数据、当前章节、情节张力
    - Layer 2: 智能检索 (~20K tokens) - 角色信息、相关章节、事件、关系
    - Layer 3: 最近上下文 (~10K tokens) - 最近章节、角色活动、关系变化
    """

    # Token estimation constant: 1 token ≈ 4 characters
    CHARS_PER_TOKEN = 4

    # Budget allocation ratios for context layers
    LAYER1_BUDGET_RATIO = 0.15  # ~5K tokens
    LAYER2_BUDGET_RATIO = 0.55  # ~20K tokens
    LAYER3_BUDGET_RATIO = 0.30  # ~10K tokens

    # Limits for content items
    MAX_MILESTONES_PER_STORYLINE = 4
    MAX_TIMELINE_NOTES = 16

    # Truncation thresholds for descriptions
    MILESTONE_DESC_TRUNCATE = 120
    TIMELINE_NOTE_DESC_TRUNCATE = 160
    CHAPTER_CONTENT_PREVIEW_TRUNCATE = 200

    # Budget thresholds for different content types
    # Characters get 60% of remaining budget before stopping
    CHARACTER_BUDGET_THRESHOLD = 0.6
    # Locations get 80% of remaining budget before stopping
    LOCATION_BUDGET_THRESHOLD = 0.8
    # Style notes get 100% of remaining budget before stopping
    STYLE_BUDGET_THRESHOLD = 1.0


    def __init__(
        self,
        bible_service: BibleService,
        storyline_manager: StorylineManager,
        relationship_engine: RelationshipEngine,
        vector_store: VectorStore,
        novel_repository: NovelRepository,
        chapter_repository: ChapterRepository,
        plot_arc_repository: Optional[PlotArcRepository] = None,
        embedding_service: Optional[EmbeddingService] = None,
        foreshadowing_repository: Optional[ForeshadowingRepository] = None,
    ):
        self.bible_service = bible_service
        self.storyline_manager = storyline_manager
        self.relationship_engine = relationship_engine
        self.vector_store = vector_store
        self.novel_repository = novel_repository
        self.chapter_repository = chapter_repository
        self.plot_arc_repository = plot_arc_repository
        self.embedding_service = embedding_service
        self.foreshadowing_repository = foreshadowing_repository

        # 创建向量检索门面（如果两个服务都可用）
        self.vector_facade = None
        if vector_store and embedding_service:
            self.vector_facade = VectorRetrievalFacade(vector_store, embedding_service)

    def build_voice_anchor_system_section(self, novel_id: str) -> str:
        """Bible 角色声线/小动作锚点，用于章节或节拍 System 提示。"""
        return self.bible_service.build_character_voice_anchor_section(novel_id)

    def build_context(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str,
        max_tokens: int = 35000
    ) -> str:
        """构建完整上下文

        Args:
            novel_id: 小说 ID
            chapter_number: 章节号
            outline: 章节大纲
            max_tokens: 最大 token 数

        Returns:
            组装好的上下文字符串
        """
        # Token 预算分配
        layer1_budget = int(max_tokens * self.LAYER1_BUDGET_RATIO)
        layer2_budget = int(max_tokens * self.LAYER2_BUDGET_RATIO)
        layer3_budget = int(max_tokens * self.LAYER3_BUDGET_RATIO)

        # Layer 1: 核心上下文
        layer1 = self._build_layer1_core_context(
            novel_id, chapter_number, outline, layer1_budget
        )

        # Layer 2: 智能检索
        layer2 = self._build_layer2_smart_retrieval(
            novel_id, chapter_number, outline, layer2_budget, scene_director=None
        )

        # Layer 3: 最近上下文
        layer3 = self._build_layer3_recent_context(
            novel_id, chapter_number, layer3_budget
        )

        # 组装上下文
        context_parts = [
            "=== CONTEXT FOR CHAPTER GENERATION ===\n",
            layer1,
            "\n=== SMART RETRIEVAL ===\n",
            layer2,
            "\n=== RECENT CONTEXT ===\n",
            layer3
        ]

        full_context = "\n".join(context_parts)

        # 如果超出预算，截断 Layer 3，然后 Layer 2
        if self.estimate_tokens(full_context) > max_tokens:
            full_context = self._truncate_to_budget(
                layer1, layer2, layer3, max_tokens
            )

        return full_context

    def build_structured_context(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str,
        max_tokens: int = 35000,
        scene_director: Optional["SceneDirectorAnalysis"] = None,
    ) -> Dict[str, Any]:
        """构建结构化上下文，分层返回

        Args:
            novel_id: 小说 ID
            chapter_number: 章节号
            outline: 章节大纲
            max_tokens: 最大 token 数
            scene_director: 可选的场记分析，用于过滤角色和地点

        Returns:
            包含分层上下文和 token 使用情况的字典
        """
        # Token 预算分配
        layer1_budget = int(max_tokens * self.LAYER1_BUDGET_RATIO)
        layer2_budget = int(max_tokens * self.LAYER2_BUDGET_RATIO)
        layer3_budget = int(max_tokens * self.LAYER3_BUDGET_RATIO)

        # Layer 1: 核心上下文
        layer1 = self._build_layer1_core_context(
            novel_id, chapter_number, outline, layer1_budget
        )

        # Layer 2: 智能检索（可选过滤）
        layer2 = self._build_layer2_smart_retrieval(
            novel_id, chapter_number, outline, layer2_budget, scene_director=scene_director
        )

        # Layer 3: 最近上下文
        layer3 = self._build_layer3_recent_context(
            novel_id, chapter_number, layer3_budget
        )

        # 计算 token 使用情况
        layer1_tokens = self.estimate_tokens(layer1)
        layer2_tokens = self.estimate_tokens(layer2)
        layer3_tokens = self.estimate_tokens(layer3)
        total_tokens = layer1_tokens + layer2_tokens + layer3_tokens

        return {
            "layer1_text": layer1,
            "layer2_text": layer2,
            "layer3_text": layer3,
            "token_usage": {
                "layer1": layer1_tokens,
                "layer2": layer2_tokens,
                "layer3": layer3_tokens,
                "total": total_tokens,
            },
        }

    def estimate_tokens(self, text: str) -> int:
        """估算 token 数量

        粗略估算：1 token ≈ 4 characters

        Args:
            text: 文本内容

        Returns:
            估算的 token 数
        """
        return len(text) // self.CHARS_PER_TOKEN

    def _build_layer1_core_context(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str,
        budget: int
    ) -> str:
        """构建 Layer 1: 核心上下文

        包含：
        - 小说元数据（标题、类型、主题）
        - 当前章节号和大纲
        - 情节弧当前张力水平
        - 活跃故事线和待完成里程碑

        Args:
            novel_id: 小说 ID
            chapter_number: 章节号
            outline: 章节大纲
            budget: token 预算

        Returns:
            Layer 1 上下文字符串
        """
        parts = []

        # 小说元数据
        nid = NovelId(novel_id)
        novel = self.novel_repository.get_by_id(nid)
        if novel:
            parts.append(f"Novel: {novel.title}")
            parts.append(f"Author: {novel.author}")

        # 当前章节
        parts.append(f"\nChapter {chapter_number}")
        parts.append(f"Outline: {outline}")

        # 活跃故事线（与当前章有章节范围交集）
        storylines = self.storyline_manager.repository.get_by_novel_id(nid)
        if storylines:
            parts.append("\nActive Storylines (for this chapter):")
            for storyline in storylines:
                if storyline.status.value != "active":
                    continue
                if not (
                    storyline.estimated_chapter_start
                    <= chapter_number
                    <= storyline.estimated_chapter_end
                ):
                    continue
                parts.append(
                    f"- {storyline.storyline_type.value}: "
                    f"Chapters {storyline.estimated_chapter_start}-{storyline.estimated_chapter_end}"
                )
                pending = storyline.get_pending_milestones()
                if pending:
                    for m in pending[:self.MAX_MILESTONES_PER_STORYLINE]:
                        desc = (m.description or "")[:self.MILESTONE_DESC_TRUNCATE]
                        parts.append(
                            f"  • Milestone #{m.order} {m.title}: {desc}"
                            + ("…" if len(m.description or "") > self.MILESTONE_DESC_TRUNCATE else "")
                        )
                    if len(pending) > self.MAX_MILESTONES_PER_STORYLINE:
                        parts.append(f"  • …and {len(pending) - self.MAX_MILESTONES_PER_STORYLINE} more pending milestones")

        # 情节弧：本章期望张力与下一锚点
        if self.plot_arc_repository is not None:
            try:
                plot_arc = self.plot_arc_repository.get_by_novel_id(nid)
                if plot_arc and plot_arc.key_points:
                    tension = plot_arc.get_expected_tension(chapter_number)
                    next_point = plot_arc.get_next_plot_point(chapter_number)
                    parts.append("\nPlot arc (pacing):")
                    parts.append(f"- Expected tension for this chapter: {tension.name} ({tension.value}/4)")
                    if next_point:
                        parts.append(
                            f"- Next plot anchor: chapter {next_point.chapter_number} — {next_point.description}"
                        )
            except Exception as e:
                logger.warning(f"Failed to load plot arc: {e}")

        # Bible 时间线笔记（世界内时间参考）
        try:
            bible_dto = self.bible_service.get_bible_by_novel(novel_id)
            if bible_dto and bible_dto.timeline_notes:
                parts.append("\nBible timeline notes (story-world time, do not contradict):")
                for note in bible_dto.timeline_notes[:self.MAX_TIMELINE_NOTES]:
                    ev = (note.event or "").strip()
                    tp = (getattr(note, "time_point", None) or "").strip()
                    desc = (note.description or "").strip()
                    line = f"- {ev}" if ev else "- (event)"
                    if tp:
                        line += f" @ {tp}"
                    if desc:
                        short = desc[:self.TIMELINE_NOTE_DESC_TRUNCATE] + ("…" if len(desc) > self.TIMELINE_NOTE_DESC_TRUNCATE else "")
                        line += f": {short}"
                    parts.append(line)
                if len(bible_dto.timeline_notes) > self.MAX_TIMELINE_NOTES:
                    parts.append(f"- …and {len(bible_dto.timeline_notes) - self.MAX_TIMELINE_NOTES} more notes")
        except Exception as e:
            logger.warning(f"Failed to load Bible timeline notes: {e}")

        # 待兑现伏笔（提醒模型呼应已埋下的线索）
        if self.foreshadowing_repository:
            try:
                reg = self.foreshadowing_repository.get_by_novel_id(NovelId(novel_id))
                if reg:
                    pending = [e for e in reg.subtext_entries if e.status == "pending"]
                    if pending:
                        parts.append("\nPending foreshadows (planted but not yet resolved — weave in or keep consistent):")
                        for entry in pending[:5]:
                            parts.append(
                                f"- Ch{entry.chapter} [{entry.character_id}]: {entry.hidden_clue}"
                            )
                        if len(pending) > 5:
                            parts.append(f"- …and {len(pending) - 5} more pending")
            except Exception as e:
                logger.warning("Failed to load foreshadowing entries: %s", e)

        context = "\n".join(parts)

        # 截断到预算
        if self.estimate_tokens(context) > budget:
            context = self._truncate_text(context, budget)

        return context

    def _build_layer2_smart_retrieval(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str,
        budget: int,
        scene_director: Optional["SceneDirectorAnalysis"] = None
    ) -> str:
        """构建 Layer 2: 智能检索

        包含：
        - Bible 中的角色信息
        - Bible 中的地点信息
        - Bible 中的风格设定
        - 相关过往章节（向量搜索）
        - 相关事件
        - 关键关系

        Args:
            novel_id: 小说 ID
            chapter_number: 章节号
            outline: 章节大纲
            budget: token 预算
            scene_director: 可选的场记分析，用于过滤角色和地点

        Returns:
            Layer 2 上下文字符串
        """
        parts = []
        running_tokens = 0
        budget_threshold_chars = budget * self.CHARS_PER_TOKEN

        # 从 Bible 获取数据
        bible_dto = self.bible_service.get_bible_by_novel(novel_id)

        if bible_dto:
            # 角色信息（支持 POV 防火墙）
            if bible_dto.characters:
                parts.append("Characters:")
                running_tokens = self.estimate_tokens("\n".join(parts))

                for char in bible_dto.characters:
                    # 如果提供了 scene_director 且指定了角色，则过滤
                    if scene_director and scene_director.characters:
                        if char.name not in scene_director.characters:
                            continue

                    # POV 防火墙：根据 reveal_chapter 决定是否包含 hidden_profile
                    profile_parts = []

                    # 优先使用 public_profile，如果为空则使用 description（向后兼容）
                    public_info = getattr(char, 'public_profile', '') or char.description
                    if public_info:
                        profile_parts.append(public_info)

                    # 检查是否应该显示 hidden_profile
                    hidden_info = getattr(char, 'hidden_profile', '')
                    reveal_chapter = getattr(char, 'reveal_chapter', None)

                    if hidden_info:
                        # reveal_chapter=None 表示总是可见
                        # 或者当前章节 >= reveal_chapter 时可见
                        if reveal_chapter is None or chapter_number >= reveal_chapter:
                            profile_parts.append(hidden_info)

                    # 组装角色信息
                    profile_text = " ".join(profile_parts) if profile_parts else ""
                    char_info = f"- {char.name}: {profile_text}"
                    char_tokens = self.estimate_tokens(char_info)

                    # 检查预算：字符预算阈值为 60%
                    if running_tokens + char_tokens > budget_threshold_chars * self.CHARACTER_BUDGET_THRESHOLD:
                        break

                    parts.append(char_info)
                    running_tokens += char_tokens

            # 地点信息
            if bible_dto.locations:
                parts.append("\nLocations:")
                running_tokens = self.estimate_tokens("\n".join(parts))

                for loc in bible_dto.locations:
                    # 如果提供了 scene_director 且指定了地点，则过滤
                    if scene_director and scene_director.locations:
                        if loc.name not in scene_director.locations:
                            continue

                    loc_info = f"- {loc.name} ({loc.location_type}): {loc.description}"
                    loc_tokens = self.estimate_tokens(loc_info)

                    # 检查预算：地点预算阈值为 80%
                    if running_tokens + loc_tokens > budget_threshold_chars * self.LOCATION_BUDGET_THRESHOLD:
                        break

                    parts.append(loc_info)
                    running_tokens += loc_tokens

            # 风格设定
            if bible_dto.style_notes:
                parts.append("\nStyle Guidelines:")
                running_tokens = self.estimate_tokens("\n".join(parts))

                for note in bible_dto.style_notes:
                    style_info = f"- {note.category}: {note.content}"
                    style_tokens = self.estimate_tokens(style_info)

                    # 检查预算：风格预算阈值为 100%
                    if running_tokens + style_tokens > budget_threshold_chars * self.STYLE_BUDGET_THRESHOLD:
                        break

                    parts.append(style_info)
                    running_tokens += style_tokens

        # 向量检索：相关章节片段（Top-5，±10 章窗口过滤）
        if self.vector_facade:
            try:
                collection_name = f"novel_{novel_id}_chunks"
                vector_results = self.vector_facade.sync_search(
                    collection=collection_name,
                    query_text=outline,
                    limit=5,
                )

                # 过滤：±10 章窗口
                filtered_results = [
                    hit for hit in vector_results
                    if abs(hit["payload"]["chapter_number"] - chapter_number) <= 10
                ]

                if filtered_results:
                    parts.append("\nRelevant Context (from previous chapters):")
                    running_tokens = self.estimate_tokens("\n".join(parts))

                    for hit in filtered_results:
                        text = hit["payload"]["text"]
                        vector_info = f"- {text}"
                        vector_tokens = self.estimate_tokens(vector_info)

                        # 检查预算
                        if running_tokens + vector_tokens > budget:
                            break

                        parts.append(vector_info)
                        running_tokens += vector_tokens

            except Exception as e:
                logger.warning(f"Vector retrieval failed: {e}")

        # 触发词与 Bible 世界设定切片联动（Phase 3 Task 5）
        # 仅当 scene_director 携带 trigger_keywords 时激活；
        # expand_triggers 将粗粒度触发词扩展为细粒度检索词集，
        # 再遍历 bible_dto.world_settings 做关键词匹配，精准注入设定片段。
        if scene_director and getattr(scene_director, "trigger_keywords", None):
            try:
                from application.engine.services.trigger_keyword_catalog import expand_triggers
                expanded_keywords = expand_triggers(list(scene_director.trigger_keywords))

                if expanded_keywords and bible_dto and getattr(bible_dto, "world_settings", None):
                    matched_settings = [
                        setting for setting in bible_dto.world_settings
                        if any(
                            kw in setting.name
                            or kw in setting.description
                            or kw in setting.setting_type
                            for kw in expanded_keywords
                        )
                    ]

                    if matched_settings:
                        parts.append("\nTriggered World Settings:")
                        running_tokens = self.estimate_tokens("\n".join(parts))

                        for setting in matched_settings:
                            setting_info = (
                                f"- [{setting.setting_type}] {setting.name}: {setting.description}"
                            )
                            setting_tokens = self.estimate_tokens(setting_info)

                            if running_tokens + setting_tokens > budget:
                                break

                            parts.append(setting_info)
                            running_tokens += setting_tokens
            except Exception as e:
                logger.warning(f"Trigger keyword Bible linkage failed: {e}")

        context = "\n".join(parts)

        # 截断到预算
        if self.estimate_tokens(context) > budget:
            context = self._truncate_text(context, budget)

        return context

    def _build_layer3_recent_context(
        self,
        novel_id: str,
        chapter_number: int,
        budget: int
    ) -> str:
        """构建 Layer 3: 最近上下文

        包含：
        - 最近 3-5 章节摘要
        - 最近角色活动
        - 最近关系变化
        - 未解决的伏笔

        Args:
            novel_id: 小说 ID
            chapter_number: 章节号
            budget: token 预算

        Returns:
            Layer 3 上下文字符串
        """
        parts = []
        running_tokens = 0

        # 最近章节
        all_chapters = self.chapter_repository.list_by_novel(NovelId(novel_id))
        recent_chapters = [c for c in all_chapters if c.number < chapter_number]
        recent_chapters = sorted(recent_chapters, key=lambda c: c.number, reverse=True)[:5]

        if recent_chapters:
            parts.append("Recent Chapters:")
            running_tokens = self.estimate_tokens("Recent Chapters:")

            for chapter in reversed(recent_chapters):  # 按时间顺序
                summary = f"Chapter {chapter.number}: {chapter.title}"
                # 添加简短内容摘要
                if chapter.content:
                    content_preview = chapter.content[:self.CHAPTER_CONTENT_PREVIEW_TRUNCATE] + "..." if len(chapter.content) > self.CHAPTER_CONTENT_PREVIEW_TRUNCATE else chapter.content
                    summary += f"\n  {content_preview}"

                summary_tokens = self.estimate_tokens(summary)

                # 检查预算
                if running_tokens + summary_tokens > budget:
                    break

                parts.append(summary)
                running_tokens += summary_tokens

        context = "\n".join(parts)

        # 截断到预算
        if self.estimate_tokens(context) > budget:
            context = self._truncate_text(context, budget)

        return context

    def _truncate_text(self, text: str, budget: int) -> str:
        """截断文本到指定 token 预算"""
        target_chars = budget * self.CHARS_PER_TOKEN
        if len(text) <= target_chars:
            return text
        return text[:target_chars] + "..."

    def _truncate_to_budget(
        self,
        layer1: str,
        layer2: str,
        layer3: str,
        max_tokens: int
    ) -> str:
        """截断上下文到预算

        优先级：Layer 1 (必须保留) > Layer 2 > Layer 3
        """
        # Layer 1 必须保留
        layer1_tokens = self.estimate_tokens(layer1)

        if layer1_tokens >= max_tokens:
            # Layer 1 已经超出预算，只能截断 Layer 1
            return self._truncate_text(layer1, max_tokens)

        remaining = max_tokens - layer1_tokens

        # 尝试添加 Layer 2
        layer2_tokens = self.estimate_tokens(layer2)
        if layer1_tokens + layer2_tokens <= max_tokens:
            # Layer 1 + Layer 2 在预算内
            remaining = max_tokens - layer1_tokens - layer2_tokens
            layer3_truncated = self._truncate_text(layer3, remaining)
            return f"{layer1}\n\n=== SMART RETRIEVAL ===\n{layer2}\n\n=== RECENT CONTEXT ===\n{layer3_truncated}"
        else:
            # Layer 2 需要截断
            layer2_truncated = self._truncate_text(layer2, remaining)
            return f"{layer1}\n\n=== SMART RETRIEVAL ===\n{layer2_truncated}"

    def magnify_outline_to_beats(self, outline: str, target_chapter_words: int = 3500) -> List[Beat]:
        """节拍放大器：将章节大纲拆分为微观节拍

        核心策略：
        1. 识别大纲中的关键动作/事件
        2. 为每个动作分配节拍，强制增加感官细节
        3. 控制单章推进速度，避免节奏过载

        Args:
            outline: 章节大纲
            target_chapter_words: 目标章节字数（默认 2500）

        Returns:
            List[Beat]: 节拍列表
        """
        beats = []

        # 简单启发式：检测大纲中的关键词
        # 后续可接入 LLM 做智能拆分
        if "争吵" in outline or "冲突" in outline or "质问" in outline:
            # 情绪冲突场景：拆分为 4 个节拍
            beats = [
                Beat(
                    description="场景氛围描写：压抑的环境、紧张的气氛、人物的微表情",
                    target_words=500,
                    focus="sensory"
                ),
                Beat(
                    description="冲突爆发：主角的质问、对方的反应、情绪的升级",
                    target_words=800,
                    focus="dialogue"
                ),
                Beat(
                    description="情绪细节：内心独白、回忆闪回、痛苦的挣扎",
                    target_words=700,
                    focus="emotion"
                ),
                Beat(
                    description="冲突结果：决裂、离开、或暂时妥协（不要轻易和好）",
                    target_words=500,
                    focus="action"
                ),
            ]
        elif "战斗" in outline or "打斗" in outline or "对决" in outline:
            # 战斗场景：拆分为 5 个节拍
            beats = [
                Beat(
                    description="战前准备：环境描写、双方对峙、紧张的气氛",
                    target_words=400,
                    focus="sensory"
                ),
                Beat(
                    description="第一回合：试探性攻击、展示能力、观察弱点",
                    target_words=600,
                    focus="action"
                ),
                Beat(
                    description="战斗升级：全力以赴、招式碰撞、环境破坏",
                    target_words=700,
                    focus="action"
                ),
                Beat(
                    description="转折点：意外发生、底牌揭露、或受伤",
                    target_words=500,
                    focus="emotion"
                ),
                Beat(
                    description="战斗结束：胜负揭晓、战后状态、后续影响",
                    target_words=300,
                    focus="action"
                ),
            ]
        elif "发现" in outline or "真相" in outline or "揭露" in outline:
            # 真相揭露场景：拆分为 3 个节拍
            beats = [
                Beat(
                    description="线索汇聚：主角回忆之前的疑点、逐步推理",
                    target_words=700,
                    focus="emotion"
                ),
                Beat(
                    description="真相揭露：关键证据出现、震惊的反应、世界观崩塌",
                    target_words=1000,
                    focus="dialogue"
                ),
                Beat(
                    description="情绪余波：接受现实、决定下一步行动",
                    target_words=800,
                    focus="emotion"
                ),
            ]
        else:
            # 默认：日常/过渡场景，拆分为 3 个节拍
            beats = [
                Beat(
                    description="场景开场：环境描写、人物登场、日常互动",
                    target_words=800,
                    focus="sensory"
                ),
                Beat(
                    description="主要事件：推进剧情的核心动作或对话",
                    target_words=1200,
                    focus="dialogue"
                ),
                Beat(
                    description="场景收尾：情绪沉淀、埋下伏笔、过渡到下一章",
                    target_words=500,
                    focus="emotion"
                ),
            ]

        # 调整字数分配，确保总和接近目标
        total_words = sum(b.target_words for b in beats)
        if total_words != target_chapter_words:
            ratio = target_chapter_words / total_words
            for beat in beats:
                beat.target_words = int(beat.target_words * ratio)

        logger.info(f"节拍放大器：将大纲拆分为 {len(beats)} 个节拍")
        return beats

    def build_beat_prompt(self, beat: Beat, beat_index: int, total_beats: int) -> str:
        """构建单个节拍的生成提示

        Args:
            beat: 节拍对象
            beat_index: 当前节拍索引（从 0 开始）
            total_beats: 总节拍数

        Returns:
            str: 节拍提示文本
        """
        focus_instructions = {
            "sensory": "重点描写感官细节：视觉（光影、色彩）、听觉（声音、静默）、触觉（温度、质感）、嗅觉、味觉。让读者身临其境。",
            "dialogue": "重点描写对话：人物的语气、表情、肢体语言、对话中的潜台词。对话要推动剧情，展现人物性格。",
            "action": "重点描写动作：具体的动作细节、力度、速度、节奏。避免抽象描述，要让读者看到画面。",
            "emotion": "重点描写情绪：内心独白、情绪的起伏、回忆闪回、心理挣扎。深入人物内心世界。",
        }

        instruction = focus_instructions.get(beat.focus, "")

        prompt = f"""
【节拍 {beat_index + 1}/{total_beats}】
目标字数：{beat.target_words} 字
聚焦点：{beat.focus}

{instruction}

节拍内容：
{beat.description}

注意：
- 这是完整章节的一部分，不要写章节标题
- 不要在节拍结尾强行总结或过渡
- 专注于当前节拍的内容，自然衔接到下一节拍
"""
        return prompt.strip()


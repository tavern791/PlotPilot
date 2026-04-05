"""AI 持续规划服务

整合宏观规划、幕级规划、AI 续规划为统一的服务
"""

import json
import uuid
import logging
from typing import Dict, List, Optional
from datetime import datetime

from domain.structure.story_node import StoryNode, NodeType, PlanningStatus, PlanningSource
from domain.structure.chapter_element import ChapterElement, ElementType, RelationType, Importance
from domain.novel.entities.chapter import Chapter, ChapterStatus
from domain.novel.value_objects.novel_id import NovelId
from domain.novel.value_objects.chapter_id import ChapterId
from domain.novel.repositories.chapter_repository import ChapterRepository
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
from infrastructure.persistence.database.chapter_element_repository import ChapterElementRepository
from domain.ai.services.llm_service import LLMService, GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from application.services.macro_merge_engine import MacroMergeEngine, MergePlan, MergeConflictException

logger = logging.getLogger(__name__)


# 导出 MergeConflictException 供路由层使用
__all__ = ['ContinuousPlanningService', 'MergeConflictException']


class ContinuousPlanningService:
    """AI 持续规划服务

    统一的规划服务，包含：
    1. 宏观规划：生成部-卷-幕结构框架
    2. 幕级规划：为指定幕生成章节规划
    3. AI 续规划：自动判断何时创建新幕
    """

    def __init__(
        self,
        story_node_repo: StoryNodeRepository,
        chapter_element_repo: ChapterElementRepository,
        llm_service: LLMService,
        bible_service=None,
        chapter_repository: Optional[ChapterRepository] = None,
    ):
        self.story_node_repo = story_node_repo
        self.chapter_element_repo = chapter_element_repo
        self.llm_service = llm_service
        self.bible_service = bible_service
        self.chapter_repository = chapter_repository

    # ==================== 宏观规划 ====================

    async def generate_macro_plan(
        self,
        novel_id: str,
        target_chapters: int,
        structure_preference: Dict[str, int]
    ) -> Dict:
        """生成宏观规划"""
        print(f"[DEBUG] 开始生成宏观规划: novel_id={novel_id}, target_chapters={target_chapters}")
        logger.info(f"Generating macro plan for novel {novel_id}")

        # 获取 Bible 信息
        print(f"[DEBUG] 获取 Bible 上下文...")
        bible_context = self._get_bible_context(novel_id)
        print(f"[DEBUG] Bible 上下文: {bible_context}")

        # 构建提示词
        print(f"[DEBUG] 构建提示词...")
        prompt = self._build_macro_planning_prompt(
            bible_context=bible_context,
            target_chapters=target_chapters,
            structure_preference=structure_preference
        )
        print(f"[DEBUG] 提示词: system={prompt.system[:100]}..., user={prompt.user[:100]}...")

        # 调用 LLM 生成规划
        print(f"[DEBUG] 调用 LLM...")
        config = GenerationConfig(max_tokens=4096, temperature=0.7)
        response = await self.llm_service.generate(prompt, config)
        print(f"[DEBUG] LLM 响应类型: {type(response)}")
        print(f"[DEBUG] LLM 响应内容: {response}")
        structure = self._parse_llm_response(response)

        return {
            "success": True,
            "structure": structure.get("parts", [])
        }

    async def confirm_macro_plan(self, novel_id: str, structure: List[Dict]) -> Dict:
        """确认宏观规划（旧版本，不安全，保留用于向后兼容）

        ⚠️ 警告：此方法不检查已有数据，可能导致僵尸节点或数据丢失
        推荐使用 confirm_macro_plan_safe() 方法
        """
        logger.warning(f"Using unsafe confirm_macro_plan for novel {novel_id}")
        logger.info(f"Confirming macro plan for novel {novel_id}")

        created_nodes = []
        order_index = 0
        part_number = 0
        volume_number = 0
        act_number = 0

        for part_data in structure:
            part_number += 1
            part_data["number"] = part_number
            part_node = self._create_node_from_data(
                novel_id, None, NodeType.PART, part_data, order_index
            )
            created_nodes.append(part_node)
            order_index += 1

            for volume_data in part_data.get("volumes", []):
                volume_number += 1
                volume_data["number"] = volume_number
                volume_node = self._create_node_from_data(
                    novel_id, part_node.id, NodeType.VOLUME, volume_data, order_index
                )
                created_nodes.append(volume_node)
                order_index += 1

                for act_data in volume_data.get("acts", []):
                    act_number += 1
                    act_data["number"] = act_number
                    act_node = self._create_node_from_data(
                        novel_id, volume_node.id, NodeType.ACT, act_data, order_index
                    )
                    created_nodes.append(act_node)
                    order_index += 1

        await self.story_node_repo.save_batch(created_nodes)

        return {
            "success": True,
            "created_nodes": len(created_nodes),
            "message": f"已创建 {len(created_nodes)} 个结构节点"
        }

    async def confirm_macro_plan_safe(self, novel_id: str, structure: List[Dict]) -> Dict:
        """安全的宏观规划确认（带血缘继承的智能合并）

        核心机制：
        1. 自底向上标记承载者（有正文的节点）
        2. 三路比对（create/update/delete）
        3. 冲突检测（红色阻断）
        4. 原子性事务执行

        Args:
            novel_id: 小说 ID
            structure: 新的宏观结构（部-卷-幕）

        Returns:
            合并结果，包含 summary（GREEN/YELLOW/RED 状态）

        Raises:
            MergeConflictException: 当新结构试图删除包含正文的节点时
        """
        logger.info(f"[SafeMerge] Starting safe macro plan confirmation for novel {novel_id}")

        # 阶段 1：深度扫描 - 获取旧结构
        old_nodes_entities = await self.story_node_repo.get_by_novel(novel_id)
        logger.info(f"[SafeMerge] Found {len(old_nodes_entities)} existing nodes")

        # 标准化旧节点：Entity → Dict（Enum 序列化）
        old_nodes = [
            {
                "id": node.id,
                "novel_id": node.novel_id,
                "parent_id": node.parent_id,
                "node_type": node.node_type.value,  # NodeType.CHAPTER → 'CHAPTER'
                "number": node.number,
                "title": node.title,
                "description": node.description,
                "order_index": node.order_index,
            }
            for node in old_nodes_entities
        ]

        # 标准化新节点：扁平化嵌套结构 → 平面列表
        new_nodes = self._flatten_structure_to_nodes(novel_id, structure)
        logger.info(f"[SafeMerge] Generated {len(new_nodes)} new nodes")

        # 阶段 2：匹配与继承 - 执行比对
        engine = MacroMergeEngine(old_nodes, new_nodes)
        plan = engine.execute_diff()
        logger.info(f"[SafeMerge] Merge plan: creates={len(plan.creates)}, updates={len(plan.updates)}, deletes={len(plan.deletes)}, conflicts={len(plan.conflicts)}")

        # 阶段 3：冲突检测 - 红色阻断
        if plan.has_fatal_conflict:
            logger.error(f"[SafeMerge] Fatal conflicts detected: {plan.conflicts}")
            raise MergeConflictException(
                message="重构导致部分已有正文的章节孤立",
                conflicts=plan.conflicts
            )

        # 阶段 4：执行合并 - 原子性事务
        logger.info(f"[SafeMerge] Applying merge plan...")
        await self.story_node_repo.apply_merge_plan(
            creates=plan.creates,
            updates=plan.updates,
            deletes=plan.deletes
        )

        logger.info(f"[SafeMerge] Merge completed successfully: {plan.summary}")
        return {
            "success": True,
            "summary": plan.summary
        }

    # ==================== 幕级规划 ====================

    async def plan_act_chapters(
        self, act_id: str, custom_chapter_count: Optional[int] = None
    ) -> Dict:
        """为指定幕生成章节规划"""
        logger.info(f"Planning chapters for act {act_id}")

        act_node = await self.story_node_repo.get_by_id(act_id)
        if not act_node:
            raise ValueError(f"幕节点不存在: {act_id}")

        bible_context = self._get_bible_context(act_node.novel_id)
        previous_summary = await self._get_previous_acts_summary(act_node)
        chapter_count = custom_chapter_count or act_node.suggested_chapter_count or 5

        prompt = self._build_act_planning_prompt(
            act_node, bible_context, previous_summary, chapter_count
        )

        response = await self.llm_service.generate(prompt, GenerationConfig(max_tokens=4096, temperature=0.7))
        plan = self._parse_llm_response(response)

        return {
            "success": True,
            "act_id": act_id,
            "chapters": plan.get("chapters", [])
        }

    async def _remove_chapter_children_of_act(self, act_id: str) -> None:
        """同一幕再次确认规划时，先删掉本幕下已有章节节点及对应正文行、元素关联，避免重复堆积。"""
        children = self.story_node_repo.get_children_sync(act_id)
        chapter_nodes = [n for n in children if n.node_type == NodeType.CHAPTER]
        for n in chapter_nodes:
            await self.chapter_element_repo.delete_by_chapter(n.id)
            if self.chapter_repository:
                self.chapter_repository.delete(ChapterId(n.id))
            await self.story_node_repo.delete(n.id)

    async def confirm_act_planning(self, act_id: str, chapters: List[Dict]) -> Dict:
        """确认幕级规划：写入 story_nodes + chapters 表（供工作台侧栏列表），并关联 Bible 元素。"""
        logger.info(f"Confirming act planning for act {act_id}")

        act_node = await self.story_node_repo.get_by_id(act_id)
        if not act_node:
            raise ValueError(f"幕节点不存在: {act_id}")

        await self._remove_chapter_children_of_act(act_id)

        novel_id_vo = NovelId(act_node.novel_id)
        existing_book = []
        if self.chapter_repository:
            existing_book = self.chapter_repository.list_by_novel(novel_id_vo)
        max_num = max((c.number for c in existing_book), default=0)
        next_global_number = max_num + 1

        created_chapters: List[StoryNode] = []
        created_elements: List[ChapterElement] = []

        for idx, raw in enumerate(chapters):
            row = self._normalize_act_chapter_row(raw, act_local_index=idx + 1)
            global_number = next_global_number + idx
            # 与 novel_service.add_chapter / 前端树选择一致：id 以 chapter-{全局章号} 结尾
            story_chapter_id = f"chapter-{act_node.novel_id}-chapter-{global_number}"

            chapter_node = StoryNode(
                id=story_chapter_id,
                novel_id=act_node.novel_id,
                parent_id=act_id,
                node_type=NodeType.CHAPTER,
                number=global_number,
                title=row["title"],
                order_index=act_node.order_index + 1 + idx,
                planning_status=PlanningStatus.CONFIRMED,
                planning_source=PlanningSource.AI_ACT,
                outline=row.get("outline"),
                pov_character_id=row.get("pov_character_id"),
            )
            created_chapters.append(chapter_node)

            elements_dict = self._merged_elements_dict(row)
            elements = self._create_elements_from_data(story_chapter_id, elements_dict)
            created_elements.extend(elements)

            if self.chapter_repository:
                book_ch = Chapter(
                    id=story_chapter_id,
                    novel_id=novel_id_vo,
                    number=global_number,
                    title=row["title"],
                    content="",
                    status=ChapterStatus.DRAFT,
                )
                self.chapter_repository.save(book_ch)

        await self.story_node_repo.save_batch(created_chapters)
        await self.chapter_element_repo.save_batch(created_elements)

        act_children = self.story_node_repo.get_children_sync(act_id)
        chapter_nodes = [n for n in act_children if n.node_type == NodeType.CHAPTER]
        if chapter_nodes:
            nums = [n.number for n in chapter_nodes]
            act_node.chapter_start = min(nums)
            act_node.chapter_end = max(nums)
        else:
            act_node.chapter_start = None
            act_node.chapter_end = None
        act_node.chapter_count = len(chapter_nodes)
        await self.story_node_repo.update(act_node)

        return {
            "success": True,
            "created_chapters": len(created_chapters),
            "created_elements": len(created_elements),
            "message": f"已写入 {len(created_chapters)} 个章节（本幕旧规划已替换）",
        }

    # ==================== AI 续规划 ====================

    async def continue_planning(self, novel_id: str, current_chapter_number: int) -> Dict:
        """AI 续规划"""
        logger.info(f"Continue planning for novel {novel_id}, chapter {current_chapter_number}")

        current_act = await self._find_act_for_chapter(novel_id, current_chapter_number)
        if not current_act:
            return {"success": False, "message": "未找到当前章节所属的幕"}

        chapters_written = await self._count_written_chapters_in_act(current_act.id)
        chapters_planned = await self._count_planned_chapters_in_act(current_act.id)

        should_end = chapters_written >= chapters_planned

        if should_end:
            next_act = await self._get_next_act(current_act)

            if next_act:
                return {
                    "success": True,
                    "act_completed": True,
                    "has_next_act": True,
                    "current_act": current_act.to_dict(),
                    "next_act": next_act.to_dict(),
                    "message": f"第 {current_act.number} 幕已完成，可以开始第 {next_act.number} 幕"
                }
            else:
                return {
                    "success": True,
                    "act_completed": True,
                    "has_next_act": False,
                    "current_act": current_act.to_dict(),
                    "suggest_create_next": True,
                    "message": f"第 {current_act.number} 幕已完成，是否需要 AI 生成下一幕？"
                }
        else:
            return {
                "success": True,
                "act_completed": False,
                "current_act": current_act.to_dict(),
                "progress": f"{chapters_written}/{chapters_planned}",
                "message": f"继续第 {current_act.number} 幕"
            }

    async def create_next_act_auto(self, novel_id: str, current_act_id: str) -> Dict:
        """自动创建下一幕"""
        logger.info(f"Creating next act after {current_act_id}")

        current_act = await self.story_node_repo.get_by_id(current_act_id)
        if not current_act:
            raise ValueError(f"当前幕不存在: {current_act_id}")

        bible_context = self._get_bible_context(novel_id)
        next_act_info = await self._generate_next_act_info(novel_id, current_act, bible_context)

        next_act = self._create_node_from_data(
            novel_id,
            current_act.parent_id,
            NodeType.ACT,
            {
                "number": current_act.number + 1,
                "title": next_act_info["title"],
                "description": next_act_info["description"],
                "suggested_chapter_count": next_act_info.get("suggested_chapter_count", 5),
                "key_events": next_act_info.get("key_events", []),
                "narrative_arc": next_act_info.get("narrative_arc"),
                "conflicts": next_act_info.get("conflicts", []),
            },
            current_act.order_index + 1
        )

        await self.story_node_repo.save(next_act)

        return {
            "success": True,
            "next_act": next_act.to_dict(),
            "message": f"已创建第 {next_act.number} 幕，请为其规划章节"
        }

    # ==================== 辅助方法 ====================

    def _get_bible_context(self, novel_id: str) -> Dict:
        """获取 Bible 上下文"""
        if not self.bible_service:
            return {}

        bible = self.bible_service.get_bible_by_novel(novel_id)
        if not bible:
            return {}

        return {
            "characters": [{"id": c.id, "name": c.name, "description": c.description}
                           for c in bible.characters],
            "world_settings": [{"id": w.id, "name": w.name, "description": w.description}
                               for w in bible.world_settings],
            "locations": [{"id": l.id, "name": l.name, "description": l.description}
                          for l in bible.locations],
            "timeline_notes": [{"id": t.id, "event": t.event, "description": t.description}
                               for t in bible.timeline_notes],
        }

    def _create_node_from_data(
        self, novel_id: str, parent_id: Optional[str], node_type: NodeType,
        data: Dict, order_index: int
    ) -> StoryNode:
        """从数据创建节点"""
        return StoryNode(
            id=f"{node_type.value}-{uuid.uuid4().hex[:8]}",
            novel_id=novel_id,
            parent_id=parent_id,
            node_type=node_type,
            number=data["number"],
            title=data["title"],
            description=data.get("description"),
            order_index=order_index,
            planning_status=PlanningStatus.CONFIRMED,
            planning_source=PlanningSource.AI_MACRO,
            suggested_chapter_count=data.get("suggested_chapter_count"),
            themes=data.get("themes", []),
            key_events=data.get("key_events", []) if node_type == NodeType.ACT else [],
            narrative_arc=data.get("narrative_arc") if node_type == NodeType.ACT else None,
            conflicts=data.get("conflicts", []) if node_type == NodeType.ACT else [],
        )

    def _flatten_structure_to_nodes(self, novel_id: str, structure: List[Dict]) -> List[Dict]:
        """将嵌套的部-卷-幕结构扁平化为节点列表（用于 MacroMergeEngine）

        Args:
            novel_id: 小说 ID
            structure: 嵌套结构 [{"title": "第一部", "volumes": [...]}]

        Returns:
            平面节点列表 [{"id": "part-xxx", "node_type": "PART", ...}]
        """
        nodes = []
        order_index = 0
        part_number = 0
        volume_number = 0
        act_number = 0

        for part_data in structure:
            part_number += 1
            part_data["number"] = part_number
            part_id = f"part-{novel_id}-{part_number}"

            nodes.append({
                "id": part_id,
                "novel_id": novel_id,
                "parent_id": None,
                "node_type": "PART",
                "number": part_number,
                "title": part_data["title"],
                "description": part_data.get("description", ""),
                "order_index": order_index,
            })
            order_index += 1

            for volume_data in part_data.get("volumes", []):
                volume_number += 1
                volume_data["number"] = volume_number
                volume_id = f"volume-{novel_id}-{volume_number}"

                nodes.append({
                    "id": volume_id,
                    "novel_id": novel_id,
                    "parent_id": part_id,
                    "node_type": "VOLUME",
                    "number": volume_number,
                    "title": volume_data["title"],
                    "description": volume_data.get("description", ""),
                    "order_index": order_index,
                })
                order_index += 1

                for act_data in volume_data.get("acts", []):
                    act_number += 1
                    act_data["number"] = act_number
                    act_id = f"act-{novel_id}-{act_number}"

                    nodes.append({
                        "id": act_id,
                        "novel_id": novel_id,
                        "parent_id": volume_id,
                        "node_type": "ACT",
                        "number": act_number,
                        "title": act_data["title"],
                        "description": act_data.get("description", ""),
                        "order_index": order_index,
                    })
                    order_index += 1

        return nodes

    def _normalize_act_chapter_row(self, raw: Dict, act_local_index: int) -> Dict:
        """LLM / 前端可能缺 number、title，或 number 为字符串；统一为可落库结构。"""
        title = (raw.get("title") or "").strip() or f"第{act_local_index}章"
        num = raw.get("number")
        try:
            num_int = int(num) if num is not None else act_local_index
        except (TypeError, ValueError):
            num_int = act_local_index
        outline = raw.get("outline") or raw.get("description") or ""
        if isinstance(outline, str):
            outline = outline.strip() or None
        else:
            outline = None
        return {
            **raw,
            "number": num_int,
            "title": title,
            "outline": outline,
        }

    def _merged_elements_dict(self, chapter_row: Dict) -> Dict:
        """提示词里人物/地点在 chapters[].characters；落库时期望 elements.characters 为带 id 的对象列表。"""
        merged: Dict = {}
        inner = chapter_row.get("elements")
        if isinstance(inner, dict):
            merged.update(inner)
        for key in ("characters", "locations"):
            top = chapter_row.get(key)
            if top and not merged.get(key):
                merged[key] = top
        return merged

    def _create_elements_from_data(self, chapter_id: str, elements_data: Dict) -> List[ChapterElement]:
        """从数据创建章节元素"""
        elements = []
        if not isinstance(elements_data, dict):
            return elements

        for char_data in elements_data.get("characters", []):
            if isinstance(char_data, str):
                char_data = {"id": char_data}
            if not isinstance(char_data, dict) or not char_data.get("id"):
                continue
            elements.append(ChapterElement(
                id=f"elem-{uuid.uuid4().hex[:8]}",
                chapter_id=chapter_id,
                element_type=ElementType.CHARACTER,
                element_id=str(char_data["id"]),
                relation_type=RelationType(char_data.get("relation", "appears")),
                importance=Importance(char_data.get("importance", "normal")),
            ))

        for loc_data in elements_data.get("locations", []):
            if isinstance(loc_data, str):
                loc_data = {"id": loc_data}
            if not isinstance(loc_data, dict) or not loc_data.get("id"):
                continue
            elements.append(ChapterElement(
                id=f"elem-{uuid.uuid4().hex[:8]}",
                chapter_id=chapter_id,
                element_type=ElementType.LOCATION,
                element_id=str(loc_data["id"]),
                relation_type=RelationType.SCENE,
                importance=Importance.NORMAL,
            ))

        return elements

    def _parse_llm_response(self, response) -> Dict:
        """解析 LLM 响应"""
        # 如果是 GenerationResult 对象，提取 content 属性
        if hasattr(response, 'content'):
            content = response.content.strip()
        else:
            content = response.strip()

        # 调试日志
        print(f"[DEBUG] LLM 原始响应: {content[:200]}...")

        # 查找 JSON 代码块
        if "```json" in content:
            # 提取 ```json 和 ``` 之间的内容
            start = content.find("```json") + 7
            end = content.find("```", start)
            if end != -1:
                content = content[start:end].strip()
        elif "```" in content:
            # 提取第一个 ``` 和最后一个 ``` 之间的内容
            start = content.find("```") + 3
            end = content.rfind("```")
            if end != -1 and end > start:
                content = content[start:end].strip()

        # 如果还有前缀文字，尝试找到 JSON 开始的位置
        if not content.startswith("{") and not content.startswith("["):
            # 查找第一个 { 或 [
            json_start = min(
                content.find("{") if "{" in content else len(content),
                content.find("[") if "[" in content else len(content)
            )
            if json_start < len(content):
                content = content[json_start:]

        print(f"[DEBUG] 清理后的内容: {content[:200]}...")

        return json.loads(content)

    def _calculate_chapter_distribution(self, total_chapters: int, parts: int) -> Dict[str, List[int]]:
        """计算黄金比例的章数分配

        核心算法：
        - 3部：25% - 50% - 25% (起源-深渊-决战)
        - 4部：20% - 30% - 30% - 20% (双峰中段)
        - 5部+：首尾各20%，中间平分剩余60%

        Returns:
            {
                "part_chapters": [250, 500, 250],  # 每部的章数
                "part_ratios": [0.25, 0.5, 0.25]   # 每部的占比
            }
        """
        if parts == 1:
            return {"part_chapters": [total_chapters], "part_ratios": [1.0]}

        if parts == 2:
            # 双部结构：40% - 60% (铺垫-高潮)
            p1 = int(total_chapters * 0.4)
            p2 = total_chapters - p1
            return {"part_chapters": [p1, p2], "part_ratios": [0.4, 0.6]}

        if parts == 3:
            # 经典三幕剧：25% - 50% - 25%
            p1 = int(total_chapters * 0.25)
            p3 = int(total_chapters * 0.25)
            p2 = total_chapters - p1 - p3
            return {"part_chapters": [p1, p2, p3], "part_ratios": [0.25, 0.5, 0.25]}

        if parts == 4:
            # 双峰中段：20% - 30% - 30% - 20%
            p1 = int(total_chapters * 0.2)
            p4 = int(total_chapters * 0.2)
            remaining = total_chapters - p1 - p4
            p2 = remaining // 2
            p3 = remaining - p2
            return {"part_chapters": [p1, p2, p3, p4], "part_ratios": [0.2, 0.3, 0.3, 0.2]}

        # 5部及以上：首尾各20%，中间平分60%
        first = int(total_chapters * 0.2)
        last = int(total_chapters * 0.2)
        middle_total = total_chapters - first - last
        middle_parts = parts - 2
        middle_each = middle_total // middle_parts

        part_chapters = [first]
        for i in range(middle_parts):
            if i == middle_parts - 1:
                # 最后一个中间部分吃掉余数
                part_chapters.append(middle_total - middle_each * (middle_parts - 1))
            else:
                part_chapters.append(middle_each)
        part_chapters.append(last)

        part_ratios = [c / total_chapters for c in part_chapters]
        return {"part_chapters": part_chapters, "part_ratios": part_ratios}

    def _build_quick_macro_prompt(self, bible_context: Dict, target_chapters: int) -> Prompt:
        """极速模式：破城槌提示词

        设计哲学：
        - 允许AI犯错，用"粗糙的泥胚"激发作者的对抗欲
        - 强调商业爆点、极端冲突、套路化节奏
        - 不给章数限制，让AI撒欢去写
        """
        system_msg = """# 角色设定
你是一位狂热且极具市场敏锐度的顶级网文主编，精通"退婚流"、"克苏鲁修仙"、"赛博朋克反乌托邦"等各种爆款商业节奏。你的任务是帮作者打破"白纸恐惧"，利用他给出的寥寥几个设定，瞬间推演填补出一个完整、宏大、且充满极端冲突的长篇叙事骨架。

# 核心推演铁律（The Icebreaker Rules）
<CONSTRAINTS>
1. 极致冲突：每一部、每一卷的结尾，必须有一个生死攸关、或世界观颠覆的"大悬念"。绝不要写平淡的日常过渡！
2. 允许刻板印象：为了快速建立骨架，你可以大胆使用经典的商业套路（如：挚友背叛、深渊凝视、高维降临、底层的逆袭反杀），但必须披上作者给定的世界观外衣。
3. 粗线条描绘：不要纠结具体的章节字数，用极具煽动性和画面感的语言描述每一幕的【核心事件】和【情绪反转】。
4. 商业节奏：第一部快速抛出核心悬念，第二部制造中段危机（主角信念崩溃或次要反派崛起），第三部爆发式收网。
</CONSTRAINTS>

# 输出要求
请直接输出JSON格式，严格包含3部，每部3卷，每卷3幕（共27幕）。
每幕（Act）的输出字段必须包含：
- "title": "具有商业爆点的幕标题（如：血染青铜门）"
- "core_conflict": "谁对抗谁？赌注是什么？（如：李明 vs 财阀执法队，赌注是妹妹的机械心脏）"
- "description": "一句话概括本幕发生了什么极具张力的事件"

不要添加任何解释性文字，不要询问额外信息。"""

        # 构建世界观上下文
        worldview_context = "【世界观与人物】\n"
        if bible_context.get("worldview"):
            worldview_context += f"世界观：{bible_context['worldview']}\n"
        if bible_context.get("characters"):
            char_names = [c.get('name', 'Unknown') for c in bible_context['characters'][:3]]
            worldview_context += f"主要人物：{', '.join(char_names)}\n"

        if worldview_context == "【世界观与人物】\n":
            worldview_context = "【世界观与人物】\n暂无详细设定，请基于通用的商业小说套路生成结构。\n"

        user_msg = f"""<WORLDVIEW_AND_PROTAGONIST>
{worldview_context}
</WORLDVIEW_AND_PROTAGONIST>

目标篇幅：约 {target_chapters} 章（供参考，不必精确分配）

请立即生成3部×3卷×3幕=27幕的叙事骨架，JSON格式：
{{
  "parts": [
    {{
      "title": "部标题（如：起源之章）",
      "volumes": [
        {{
          "title": "卷标题（如：觉醒）",
          "acts": [
            {{
              "title": "幕标题（如：血染青铜门）",
              "core_conflict": "冲突描述",
              "description": "情节摘要"
            }}
          ]
        }}
      ]
    }}
  ]
}}"""
        return Prompt(system=system_msg, user=user_msg)

    def _build_precise_macro_prompt(self, bible_context: Dict, target_chapters: int, structure_preference: Dict) -> Prompt:
        """精密模式：手术刀提示词

        设计哲学：
        - 捍卫创作者的绝对主权与节奏感
        - 严格遵守用户指定的结构网格和字数节奏
        - 强调逻辑严密、中段支撑、情节容量匹配
        """
        parts = structure_preference.get('parts', 3)
        volumes_per_part = structure_preference.get('volumes_per_part', 3)
        acts_per_volume = structure_preference.get('acts_per_volume', 3)

        # 计算章数分配
        distribution = self._calculate_chapter_distribution(target_chapters, parts)
        part_chapters = distribution["part_chapters"]

        # 构建动态章节配额指示
        pacing_guide = "<PACING_GUIDE>\n"
        for i, chapters in enumerate(part_chapters, 1):
            if i == 1:
                pacing_guide += f"- 第{i}部（起源）：分配 {chapters} 章。情节要求：紧凑、抛出核心悬念。\n"
            elif i == parts:
                pacing_guide += f"- 第{i}部（决战）：分配 {chapters} 章。情节要求：收束所有主线，节奏极快。\n"
            else:
                pacing_guide += f"- 第{i}部（发展/深渊）：分配 {chapters} 章。情节要求：容量极大，需要多方势力博弈。\n"
        pacing_guide += "</PACING_GUIDE>"

        system_msg = f"""# 角色设定
你是一位极其理性的长篇小说结构架构师。作者正在进行一项严密的叙事工程。他设定了极其精确的篇幅限制和结构分布。你的任务是像外科手术一样，在严格遵守作者给定的"结构网格"和"字数节奏"的前提下，分配情节张力，确保中段不塌陷，高潮不疲软。

# 精密推演铁律（The Scalpel Rules）
<CONSTRAINTS>
1. 节奏匹配：系统已为你计算好每部的【预估章数配额】（见下方指示）。你设计的情节容量，必须能撑起这个章数！
   - 例如：如果某部分配了150章，你必须设计【多线叙事】或【复杂的长线副本】；如果某部只有30章，情节必须是【单线程的快速高潮】。
2. 逻辑严密：严禁使用突兀的机械降神。所有的转折必须基于现有的设定进行逻辑推演。
3. 中段支撑：在中间部分（非首尾）必须设计"次要反派的崛起"或"主角信念的暂时崩溃"，以维持长篇连载的订阅张力。
4. 结构网格绝对服从：必须严格输出 {parts} 部 × {volumes_per_part} 卷/部 × {acts_per_volume} 幕/卷，不得多一个或少一个节点。
</CONSTRAINTS>

{pacing_guide}

# 输出要求
请直接输出JSON格式，层级必须严格吻合结构网格。
每幕（Act）的输出字段必须包含：
- "title": "精准的情节标题"
- "estimated_chapters": 本幕预计占用的章数（请参考<PACING_GUIDE>合理分配）
- "narrative_goal": "本幕在整个故事结构中承担的叙事功能（如：引出副线、主角遭遇重大挫折）"
- "description": "严谨的剧情走向摘要"

不要添加任何解释性文字，不要询问额外信息。"""

        # 构建世界观上下文
        worldview_context = "【世界观与人物】\n"
        if bible_context.get("worldview"):
            worldview_context += f"世界观：{bible_context['worldview']}\n"
        if bible_context.get("characters"):
            char_list = [f"- {c.get('name', 'Unknown')} (ID: {c.get('id', 'N/A')})" for c in bible_context['characters'][:5]]
            worldview_context += "可用人物：\n" + "\n".join(char_list) + "\n"
        if bible_context.get("locations"):
            loc_list = [f"- {l.get('name', 'Unknown')} (ID: {l.get('id', 'N/A')})" for l in bible_context['locations'][:5]]
            worldview_context += "可用地点：\n" + "\n".join(loc_list) + "\n"

        if worldview_context == "【世界观与人物】\n":
            worldview_context = "【世界观与人物】\n暂无详细设定，请生成通用的结构框架。\n"

        user_msg = f"""<STORY_CONTEXT>
{worldview_context}
</STORY_CONTEXT>

<STRUCTURAL_GRID>
【你必须绝对服从的物理网格】
- 目标总章数：{target_chapters} 章
- 结构分布：{parts} 部 × {volumes_per_part} 卷/部 × {acts_per_volume} 幕/卷
</STRUCTURAL_GRID>

请生成严格符合上述网格的叙事结构，JSON格式：
{{
  "parts": [
    {{
      "title": "部标题",
      "volumes": [
        {{
          "title": "卷标题",
          "acts": [
            {{
              "title": "幕标题",
              "estimated_chapters": 25,
              "narrative_goal": "叙事功能",
              "description": "剧情摘要"
            }}
          ]
        }}
      ]
    }}
  ]
}}"""
        return Prompt(system=system_msg, user=user_msg)

    def _build_macro_planning_prompt(self, bible_context: Dict, target_chapters: int, structure_preference: Dict) -> Prompt:
        """构建宏观规划提示词（向后兼容的包装器）

        根据 structure_preference 是否为 None 来判断模式：
        - None: 极速模式（AI自主决定）
        - Dict: 精密模式（用户指定结构）
        """
        if structure_preference is None:
            # 极速模式：使用默认的3×3×3结构
            return self._build_quick_macro_prompt(bible_context, target_chapters)
        else:
            # 精密模式：使用用户指定的结构
            return self._build_precise_macro_prompt(bible_context, target_chapters, structure_preference)

    def _build_act_planning_prompt(self, act_node: StoryNode, bible_context: Dict, previous_summary: Optional[str], chapter_count: int) -> Prompt:
        """构建幕级规划提示词"""
        system_msg = """你是一个专业的小说章节规划助手，擅长设计章节大纲和情节安排。
你的任务是根据提供的信息生成章节规划，即使信息不完整也要生成合理的框架。
请直接输出 JSON 格式，不要询问额外信息，不要添加任何解释性文字。"""

        # 构建上下文信息
        context_parts = [f"幕信息：《{act_node.title}》"]
        if act_node.description:
            context_parts.append(f"幕简介：{act_node.description}")

        if previous_summary:
            context_parts.append(f"\n前情提要：{previous_summary}")

        # 添加 Bible 信息
        if bible_context.get("characters"):
            char_list = [f"- {c.get('name', 'Unknown')} (ID: {c.get('id', 'N/A')})" for c in bible_context["characters"][:5]]
            context_parts.append(f"\n可用人物：\n" + "\n".join(char_list))

        if bible_context.get("locations"):
            loc_list = [f"- {l.get('name', 'Unknown')} (ID: {l.get('id', 'N/A')})" for l in bible_context["locations"][:5]]
            context_parts.append(f"\n可用地点：\n" + "\n".join(loc_list))

        context = "\n".join(context_parts)

        user_msg = f"""{context}

请为这一幕规划 {chapter_count} 个章节。如果没有详细的世界观信息，请生成通用的章节框架。

要求：
1. 每个章节需要有标题和大纲
2. 如果有可用的人物和地点，尽量关联；如果没有，可以留空
3. 章节编号从 1 开始递增

请直接输出 JSON 格式，不要添加任何说明文字：
{{
  "chapters": [
    {{
      "number": 1,
      "title": "章节标题",
      "outline": "章节大纲（100-200字）",
      "characters": ["人物ID"],
      "locations": ["地点ID"]
    }}
  ]
}}"""
        return Prompt(system=system_msg, user=user_msg)

    async def _get_previous_acts_summary(self, act_node: StoryNode) -> Optional[str]:
        """获取前面幕的摘要"""
        return None

    async def _find_act_for_chapter(self, novel_id: str, chapter_number: int) -> Optional[StoryNode]:
        """查找章节所属的幕"""
        tree = self.story_node_repo.get_tree(novel_id)
        acts = [n for n in tree.nodes if n.node_type == NodeType.ACT]
        return max(acts, key=lambda x: x.number) if acts else None

    async def _count_written_chapters_in_act(self, act_id: str) -> int:
        """统计已写章节数"""
        children = self.story_node_repo.get_children(act_id, None)
        return sum(1 for n in children if n.node_type == NodeType.CHAPTER and n.word_count and n.word_count > 0)

    async def _count_planned_chapters_in_act(self, act_id: str) -> int:
        """统计已规划章节数"""
        children = self.story_node_repo.get_children(act_id, None)
        return sum(1 for n in children if n.node_type == NodeType.CHAPTER)

    async def _get_next_act(self, current_act: StoryNode) -> Optional[StoryNode]:
        """获取下一幕"""
        tree = self.story_node_repo.get_tree(current_act.novel_id)
        acts = [n for n in tree.nodes if n.node_type == NodeType.ACT and n.number == current_act.number + 1]
        return acts[0] if acts else None

    async def _generate_next_act_info(self, novel_id: str, current_act: StoryNode, bible_context: Dict) -> Dict:
        """生成下一幕信息"""
        prompt = Prompt(
            system="你是一个专业的小说结构规划助手。",
            user=f"""生成第{current_act.number + 1}幕信息。输出JSON格式：{{"title": "幕标题", "description": "幕简介"}}"""
        )
        try:
            response = await self.llm_service.generate(prompt, GenerationConfig(max_tokens=4096, temperature=0.7))
            return self._parse_llm_response(response)
        except:
            return {
                "title": f"第{current_act.number + 1}幕",
                "description": "描述",
                "suggested_chapter_count": 5
            }

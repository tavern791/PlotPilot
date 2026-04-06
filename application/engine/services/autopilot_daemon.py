"""自动驾驶守护进程 v2 - 全托管写作引擎（事务最小化 + 节拍幂等）

核心设计：
1. 死循环轮询数据库，捞出所有 autopilot_status=RUNNING 的小说
2. 根据 current_stage 执行对应的状态机逻辑
3. 事务最小化：DB 写操作只在读状态和更新状态两个瞬间，LLM 请求期间不持有锁
4. 节拍级幂等：每写完一个节拍立刻落库，断点续写从 current_beat_index 恢复
5. 熔断保护：连续失败 3 次挂起单本小说，全局熔断器防止 API 雪崩
"""
import time
import logging
import asyncio
from typing import Any, Dict, List, Optional

from domain.novel.entities.novel import Novel, NovelStage, AutopilotStatus
from domain.novel.value_objects.novel_id import NovelId
from domain.novel.repositories.novel_repository import NovelRepository
from domain.ai.services.llm_service import LLMService, GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from application.engine.services.context_builder import ContextBuilder
from application.engine.services.background_task_service import BackgroundTaskService, TaskType
from domain.novel.value_objects.chapter_id import ChapterId

logger = logging.getLogger(__name__)


class AutopilotDaemon:
    """自动驾驶守护进程（v2 完整实现）"""

    def __init__(
        self,
        novel_repository,
        llm_service,
        context_builder,
        background_task_service,
        planning_service,
        story_node_repo,
        chapter_repository,
        poll_interval: int = 5,
        voice_drift_service=None,
        circuit_breaker=None,
    ):
        self.novel_repository = novel_repository
        self.llm_service = llm_service
        self.context_builder = context_builder
        self.background_task_service = background_task_service
        self.planning_service = planning_service
        self.story_node_repo = story_node_repo
        self.chapter_repository = chapter_repository
        self.poll_interval = poll_interval
        self.voice_drift_service = voice_drift_service
        self.circuit_breaker = circuit_breaker

    def run_forever(self):
        """守护进程主循环（事务最小化原则）"""
        logger.info("=" * 80)
        logger.info("🚀 Autopilot Daemon Started")
        logger.info(f"   Poll Interval: {self.poll_interval}s")
        logger.info(f"   Circuit Breaker: {'Enabled' if self.circuit_breaker else 'Disabled'}")
        logger.info(f"   Voice Drift Service: {'Enabled' if self.voice_drift_service else 'Disabled'}")
        logger.info("=" * 80)

        loop_count = 0
        while True:
            loop_count += 1
            loop_start = time.time()

            # 熔断器检查
            if self.circuit_breaker and self.circuit_breaker.is_open():
                wait = self.circuit_breaker.wait_seconds()
                logger.warning(f"⚠️  熔断器打开，暂停 {wait:.0f}s")
                time.sleep(min(wait, self.poll_interval))
                continue

            try:
                active_novels = self._get_active_novels()  # 快速只读查询

                if loop_count % 10 == 1:  # 每10轮（约50秒）记录一次状态
                    logger.info(f"🔄 Loop #{loop_count}: 发现 {len(active_novels)} 本活跃小说")

                if active_novels:
                    for novel in active_novels:
                        novel_start = time.time()
                        asyncio.run(self._process_novel(novel))
                        novel_elapsed = time.time() - novel_start
                        logger.debug(f"   [{novel.novel_id}] 处理耗时: {novel_elapsed:.2f}s")

            except Exception as e:
                logger.error(f"❌ Daemon 顶层异常: {e}", exc_info=True)

            loop_elapsed = time.time() - loop_start
            if loop_elapsed > self.poll_interval * 2:
                logger.warning(f"⏱️  Loop #{loop_count} 耗时过长: {loop_elapsed:.2f}s")

            time.sleep(self.poll_interval)

    def _get_active_novels(self) -> List[Novel]:
        """获取所有活跃小说（快速只读）"""
        return self.novel_repository.find_by_autopilot_status(AutopilotStatus.RUNNING.value)

    def _merge_autopilot_status_from_db(self, novel: Novel) -> None:
        """用户点「停止」只改 DB；写库前必须合并，否则会覆盖 STOPPED。"""
        fresh = self.novel_repository.get_by_id(novel.novel_id)
        if fresh:
            novel.autopilot_status = fresh.autopilot_status

    def _is_still_running(self, novel: Novel) -> bool:
        """从 DB 同步自动驾驶状态；非 RUNNING 时应立即结束本段处理。"""
        self._merge_autopilot_status_from_db(novel)
        return novel.autopilot_status == AutopilotStatus.RUNNING

    def _flush_novel(self, novel: Novel) -> None:
        """关键阶段立即写库，避免下一轮轮询仍读到旧 stage（重复幕级规划 / 重复日志）。"""
        self._merge_autopilot_status_from_db(novel)
        self.novel_repository.save(novel)

    def _save_novel_state(self, novel: Novel) -> None:
        """与 _flush_novel 相同语义：任意 save 前合并停止标志。"""
        self._merge_autopilot_status_from_db(novel)
        self.novel_repository.save(novel)

    async def _process_novel(self, novel: Novel):
        """处理单个小说（全流程）"""
        try:
            if not self._is_still_running(novel):
                logger.info(f"[{novel.novel_id}] 用户已停止自动驾驶，跳过本轮")
                return

            stage_name = novel.current_stage.value
            logger.debug(f"[{novel.novel_id}] 当前阶段: {stage_name}")

            if novel.current_stage == NovelStage.MACRO_PLANNING:
                logger.info(f"[{novel.novel_id}] 📋 开始宏观规划")
                await self._handle_macro_planning(novel)
            elif novel.current_stage == NovelStage.ACT_PLANNING:
                logger.info(f"[{novel.novel_id}] 📝 开始幕级规划 (第 {novel.current_act + 1} 幕)")
                await self._handle_act_planning(novel)
            elif novel.current_stage == NovelStage.WRITING:
                logger.info(f"[{novel.novel_id}] ✍️  开始写作 (第 {novel.current_act + 1} 幕)")
                await self._handle_writing(novel)
            elif novel.current_stage == NovelStage.AUDITING:
                logger.info(f"[{novel.novel_id}] 🔍 开始审计")
                await self._handle_auditing(novel)
            elif novel.current_stage == NovelStage.PAUSED_FOR_REVIEW:
                logger.debug(f"[{novel.novel_id}] ⏸️  等待人工审阅")
                return  # 人工干预点：不处理，等前端确认

            # ✅ 收尾写库（合并 DB 停止标志，避免把用户「停止」写回 RUNNING）
            self._merge_autopilot_status_from_db(novel)
            if novel.autopilot_status == AutopilotStatus.RUNNING:
                if self.circuit_breaker:
                    self.circuit_breaker.record_success()
                novel.consecutive_error_count = 0
            else:
                logger.info(f"[{novel.novel_id}] 💾 本轮结束（用户已停止，不再计成功/重置熔断）")
            self._save_novel_state(novel)
            logger.debug(f"[{novel.novel_id}] 💾 状态已保存")

        except Exception as e:
            logger.error(f"❌ [{novel.novel_id}] 处理失败: {e}", exc_info=True)

            self._merge_autopilot_status_from_db(novel)
            if novel.autopilot_status != AutopilotStatus.RUNNING:
                logger.info(f"[{novel.novel_id}] 处理异常但用户已停止，不累计熔断/失败次数")
                self._save_novel_state(novel)
                return

            # 熔断器：记录失败
            if self.circuit_breaker:
                self.circuit_breaker.record_failure()
            novel.consecutive_error_count = (novel.consecutive_error_count or 0) + 1

            if novel.consecutive_error_count >= 3:
                # 单本小说连续 3 次错误 → 挂起（不影响其他小说）
                logger.error(f"🚨 [{novel.novel_id}] 连续失败 {novel.consecutive_error_count} 次，挂起等待急救")
                novel.autopilot_status = AutopilotStatus.ERROR
            else:
                logger.warning(f"⚠️  [{novel.novel_id}] 连续失败 {novel.consecutive_error_count}/3 次")
            self._save_novel_state(novel)

    async def _handle_macro_planning(self, novel: Novel):
        """处理宏观规划（规划部/卷/幕）"""
        if not self._is_still_running(novel):
            return

        target_chapters = novel.target_chapters or 30
        structure_preference = {
            "parts": 1,
            "volumes_per_part": 1,
            "acts_per_volume": 3,
            "chapters_per_act": max(target_chapters // 3, 5)
        }

        result = await self.planning_service.generate_macro_plan(
            novel_id=novel.novel_id.value,
            target_chapters=target_chapters,
            structure_preference=structure_preference
        )

        if not self._is_still_running(novel):
            logger.info(f"[{novel.novel_id}] 宏观规划 LLM 返回后检测到停止，不再落库")
            return

        struct = result.get("structure") if isinstance(result, dict) else None
        # 注意：structure 为 [] 时不能写 `if result.get("structure")`，否则会被当成失败分支且不落库
        if result.get("success") and isinstance(struct, list) and len(struct) > 0:
            await self._confirm_macro_structure(novel, struct)
        else:
            logger.warning(
                f"[{novel.novel_id}] 宏观规划未返回有效结构（success={result.get('success')!r}），使用最小占位结构"
            )
            await self._create_minimal_structure(novel)

        # ⏸ 幕级大纲已就绪，进入人工审阅点（先落库再记日志，防止未保存导致下轮仍跑宏观规划）
        novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
        self._flush_novel(novel)
        logger.info(f"[{novel.novel_id}] 宏观规划完成，进入审阅等待")

    async def _confirm_macro_structure(self, novel: Novel, structure: list):
        """落库宏观结构；安全合并失败时回退为一次性写入（新书通常为无冲突）。"""
        novel_id = novel.novel_id.value
        try:
            await self.planning_service.confirm_macro_plan_safe(
                novel_id=novel_id,
                structure=structure
            )
        except Exception as e:
            logger.warning(f"[{novel_id}] confirm_macro_plan_safe 失败，回退 confirm_macro_plan：{e}")
            await self.planning_service.confirm_macro_plan(
                novel_id=novel_id,
                structure=structure
            )

    async def _create_minimal_structure(self, novel: Novel):
        """LLM 无输出或解析为空时，落库最小部-卷-幕树，避免审阅点侧栏仍为空。"""
        novel_id = novel.novel_id.value
        target = novel.target_chapters or 30
        per_act = max(target // 3, 5)
        structure = [{
            "title": "第一部",
            "description": "全托管自动生成的占位结构（可在审阅后于结构树中调整）",
            "volumes": [{
                "title": "第一卷",
                "description": "",
                "acts": [
                    {
                        "title": "第一幕 · 开端",
                        "description": "故事建立与主线引出",
                        "suggested_chapter_count": per_act,
                    },
                    {
                        "title": "第二幕 · 发展",
                        "description": "冲突升级与转折",
                        "suggested_chapter_count": per_act,
                    },
                    {
                        "title": "第三幕 · 高潮与收尾",
                        "description": "决战与结局",
                        "suggested_chapter_count": per_act,
                    },
                ],
            }],
        }]
        logger.warning(f"[{novel.novel_id}] 使用最小占位宏观结构（{len(structure[0]['volumes'][0]['acts'])} 幕）")
        await self._confirm_macro_structure(novel, structure)

    def _fallback_act_chapters_plan(self, act_node, count: int) -> List[Dict[str, Any]]:
        """LLM 幕级规划失败或 chapters 为空时，生成可落库的占位章节（避免抛错导致连续失败计数）。"""
        n = max(int(count or 5), 1)
        act_num = getattr(act_node, "number", None) or 1
        act_label = (getattr(act_node, "title", None) or f"第{act_num}幕").strip()
        rows: List[Dict[str, Any]] = []
        for i in range(n):
            rows.append({
                "title": f"{act_label} · 第{i + 1}章（占位）",
                "outline": (
                    f"【占位】{act_label} 第 {i + 1} 章：推进本幕叙事；"
                    "可在结构树中修改或重新运行幕级规划。"
                ),
            })
        return rows

    async def _handle_act_planning(self, novel: Novel):
        """处理幕级规划（插入缓冲章策略）"""
        if not self._is_still_running(novel):
            return

        novel_id = novel.novel_id.value
        target_act_number = novel.current_act + 1  # 1-indexed

        all_nodes = await self.story_node_repo.get_by_novel(novel_id)
        act_nodes = sorted(
            [n for n in all_nodes if n.node_type.value == "act"],
            key=lambda n: n.number
        )

        target_act = next((n for n in act_nodes if n.number == target_act_number), None)

        if not target_act:
            if act_nodes:
                await self.planning_service.create_next_act_auto(
                    novel_id=novel_id,
                    current_act_id=act_nodes[-1].id
                )
                all_nodes = await self.story_node_repo.get_by_novel(novel_id)
                act_nodes = sorted(
                    [n for n in all_nodes if n.node_type.value == "act"],
                    key=lambda n: n.number
                )
                target_act = next((n for n in act_nodes if n.number == target_act_number), None)

            if not target_act:
                logger.error(f"[{novel.novel_id}] 找不到第 {target_act_number} 幕")
                novel.current_stage = NovelStage.WRITING
                return

        # 检查该幕下是否已有章节节点（避免重复规划）
        act_children = self.story_node_repo.get_children_sync(target_act.id)
        confirmed_chapters = [n for n in act_children if n.node_type.value == "chapter"]

        just_created_chapter_plan = False
        if not confirmed_chapters:
            chapter_budget = target_act.suggested_chapter_count or 5
            plan_result: Dict[str, Any] = {}
            try:
                plan_result = await self.planning_service.plan_act_chapters(
                    act_id=target_act.id,
                    custom_chapter_count=chapter_budget
                )
            except Exception as e:
                logger.warning(
                    f"[{novel.novel_id}] plan_act_chapters 未捕获异常: {e}",
                    exc_info=True,
                )
                plan_result = {}

            if not self._is_still_running(novel):
                logger.info(f"[{novel.novel_id}] 幕级规划返回后检测到停止，不再落库")
                return

            raw = plan_result.get("chapters")
            chapters_data: List[Dict[str, Any]] = raw if isinstance(raw, list) else []
            if not chapters_data:
                logger.warning(
                    f"[{novel.novel_id}] 幕 {target_act_number} 未得到有效章节规划，使用占位章节落库"
                )
                chapters_data = self._fallback_act_chapters_plan(target_act, chapter_budget)

            await self.planning_service.confirm_act_planning(
                act_id=target_act.id,
                chapters=chapters_data
            )
            just_created_chapter_plan = True

        act_children = self.story_node_repo.get_children_sync(target_act.id)
        confirmed_chapters = [n for n in act_children if n.node_type.value == "chapter"]

        # current_act 为 0-based 幕索引（与 Novel 实体一致），勿写入 1-based 的 target_act_number
        novel.current_act = target_act_number - 1

        if not confirmed_chapters:
            logger.error(
                f"[{novel.novel_id}] 幕 {target_act_number} 仍无章节节点，下轮继续幕级规划"
            )
            novel.current_stage = NovelStage.ACT_PLANNING
            return

        # 仅在本轮「新落库」幕级章节规划时暂停审阅；用户确认后同幕已有节点则直接写作，避免反复弹审批
        if just_created_chapter_plan:
            novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
            self._flush_novel(novel)
            logger.info(f"[{novel.novel_id}] 第 {target_act_number} 幕规划完成，进入审阅等待")
        else:
            novel.current_stage = NovelStage.WRITING
            self._flush_novel(novel)
            logger.info(
                f"[{novel.novel_id}] 第 {target_act_number} 幕章节节点已存在，进入写作"
            )

    async def _handle_writing(self, novel: Novel):
        """处理写作（节拍级幂等落库）"""
        if not self._is_still_running(novel):
            return

        # 1. 成本控制：达到最大章节数则自动停止
        max_chapters = novel.max_auto_chapters or 50
        if (novel.current_auto_chapters or 0) >= max_chapters:
            logger.info(f"[{novel.novel_id}] 已达成本控制上限 {max_chapters} 章，自动停止")
            novel.autopilot_status = AutopilotStatus.STOPPED
            novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
            return

        # 2. 缓冲章判断（高潮后插入日常章）
        needs_buffer = (novel.last_chapter_tension or 0) >= 8
        if needs_buffer:
            logger.info(f"[{novel.novel_id}] 上章张力≥8，强制生成缓冲章")

        # 3. 找下一个未写章节
        next_chapter_node = await self._find_next_unwritten_chapter_async(novel)
        if not next_chapter_node:
            if await self._current_act_fully_written(novel):
                novel.current_act += 1
                novel.current_chapter_in_act = 0
                novel.current_stage = NovelStage.ACT_PLANNING
            else:
                novel.current_stage = NovelStage.AUDITING
            return

        chapter_num = next_chapter_node.number
        outline = next_chapter_node.outline or next_chapter_node.description or next_chapter_node.title

        if needs_buffer:
            outline = f"【缓冲章：日常过渡】{outline}。主角战后休整，与配角闲聊，展示收获，节奏轻松。"

        logger.info(f"[{novel.novel_id}] 📖 开始写第 {chapter_num} 章：{outline[:60]}...")
        logger.info(f"[{novel.novel_id}]    进度: {novel.current_auto_chapters or 0}/{novel.max_auto_chapters or 50} 章")

        if not self._is_still_running(novel):
            logger.info(f"[{novel.novel_id}] 用户已停止，跳过本章（上下文组装前）")
            return

        # 4. 组装上下文（不持有数据库锁，纯读操作）
        context = ""
        if self.context_builder:
            try:
                context = self.context_builder.build_context(
                    novel_id=novel.novel_id.value,
                    chapter_number=chapter_num,
                    outline=outline,
                    max_tokens=20000
                )
            except Exception as e:
                logger.warning(f"ContextBuilder 失败，降级：{e}")

        # 5. 节拍放大
        beats = []
        if self.context_builder:
            beats = self.context_builder.magnify_outline_to_beats(outline, target_chapter_words=2500)

        # 6. 🔑 节拍级幂等生成 + 增量落库
        start_beat = novel.current_beat_index or 0  # 断点续写：从上次中断的节拍继续

        chapter_content = await self._get_existing_chapter_content(novel, chapter_num) or ""

        if beats:
            for i, beat in enumerate(beats):
                if i < start_beat:
                    continue  # 跳过已生成的节拍

                if not self._is_still_running(novel):
                    logger.info(f"[{novel.novel_id}] 用户已停止，中断本章（节拍 {i + 1}/{len(beats)} 前）")
                    return

                beat_prompt = self.context_builder.build_beat_prompt(beat, i, len(beats))
                beat_content = await self._stream_one_beat(outline, context, beat_prompt, beat, novel=novel)

                if beat_content.strip():
                    chapter_content += ("\n\n" if chapter_content else "") + beat_content
                    await self._upsert_chapter_content(novel, next_chapter_node, chapter_content, status="draft")

                if not self._is_still_running(novel):
                    novel.current_beat_index = i
                    self._flush_novel(novel)
                    logger.info(
                        f"[{novel.novel_id}] 用户已停止，中断节拍 {i + 1}/{len(beats)}"
                        + ("（已保存已输出片段）" if beat_content.strip() else "（未产生文本）")
                    )
                    return

                novel.current_beat_index = i + 1
                self._flush_novel(novel)

                logger.info(f"[{novel.novel_id}]    ✅ 节拍 {i+1}/{len(beats)} 完成: {len(beat_content)} 字")
        else:
            # 降级：无节拍，一次生成
            if not self._is_still_running(novel):
                logger.info(f"[{novel.novel_id}] 用户已停止，跳过单段生成")
                return
            beat_content = await self._stream_one_beat(outline, context, None, None, novel=novel)
            if not self._is_still_running(novel):
                logger.info(f"[{novel.novel_id}] 用户已停止，单段生成已中断")
                novel.current_beat_index = 0
                self._flush_novel(novel)
                return
            chapter_content += beat_content
            await self._upsert_chapter_content(novel, next_chapter_node, chapter_content, status="draft")

        if not self._is_still_running(novel):
            logger.info(f"[{novel.novel_id}] 用户已停止，本章不标记完成")
            self._flush_novel(novel)
            return

        # 7. 章节完成，标记 completed
        await self._upsert_chapter_content(novel, next_chapter_node, chapter_content, status="completed")

        # 8. 更新计数器，重置节拍索引
        novel.current_auto_chapters = (novel.current_auto_chapters or 0) + 1
        novel.current_chapter_in_act += 1
        novel.current_beat_index = 0
        novel.current_stage = NovelStage.AUDITING

        logger.info(f"[{novel.novel_id}] 🎉 第 {chapter_num} 章完成：{len(chapter_content)} 字 (共 {novel.current_auto_chapters}/{novel.max_auto_chapters or 50} 章)")

    async def _handle_auditing(self, novel: Novel):
        """处理审计（含张力打分）"""
        if not self._is_still_running(novel):
            return

        chapter_num = novel.current_act * 10 + novel.current_chapter_in_act  # 刚写完的章节

        from domain.novel.value_objects.novel_id import NovelId
        from domain.novel.value_objects.chapter_id import ChapterId

        chapter = self.chapter_repository.get_by_novel_and_number(
            NovelId(novel.novel_id.value), chapter_num
        )
        if not chapter:
            novel.current_stage = NovelStage.WRITING
            return

        content = chapter.content or ""
        chapter_id = ChapterId(chapter.id)

        # 1. 提交后台异步任务（不阻塞）
        for task_type in [TaskType.VOICE_ANALYSIS, TaskType.GRAPH_UPDATE, TaskType.FORESHADOW_EXTRACT]:
            self.background_task_service.submit_task(
                task_type=task_type,
                novel_id=novel.novel_id,
                chapter_id=chapter_id,
                payload={"content": content, "chapter_number": chapter_num}
            )

        # 2. 张力打分（轻量 LLM 调用，~200 token）
        tension = await self._score_tension(content)
        novel.last_chapter_tension = tension
        logger.info(f"[{novel.novel_id}] 章节 {chapter_num} 张力值：{tension}/10")

        # 3. 文风漂移检测（同步）
        drift_too_high = False
        if self.voice_drift_service and content:
            try:
                result = self.voice_drift_service.score_chapter(
                    novel_id=novel.novel_id.value,
                    chapter_number=chapter_num,
                    content=content
                )
                similarity = result.get("similarity_score")
                drift_alert = result.get("drift_alert", False)
                logger.info(f"[{novel.novel_id}] 文风相似度：{similarity}，告警：{drift_alert}")
                drift_too_high = drift_alert
            except Exception as e:
                logger.warning(f"文风检测失败（跳过）：{e}")

        # 4. 文风漂移 → 删章重写
        if drift_too_high:
            logger.warning(f"[{novel.novel_id}] 章节 {chapter_num} 文风漂移，删章重写")
            self.chapter_repository.delete(chapter_id)
            novel.current_chapter_in_act -= 1
            novel.current_auto_chapters = max(0, (novel.current_auto_chapters or 1) - 1)
            novel.current_beat_index = 0
            novel.current_stage = NovelStage.WRITING
            return

        novel.current_stage = NovelStage.WRITING

        # 5. 全书完成检测
        chapters = self.chapter_repository.list_by_novel(NovelId(novel.novel_id.value))
        completed = [c for c in chapters if c.status.value == "completed"]
        if len(completed) >= novel.target_chapters:
            logger.info(f"[{novel.novel_id}] 🎉 全书完成！共 {len(completed)} 章")
            novel.autopilot_status = AutopilotStatus.STOPPED
            novel.current_stage = NovelStage.COMPLETED

    async def _score_tension(self, content: str) -> int:
        """给章节打张力分（1-10），用于判断是否插入缓冲章"""
        if not content or len(content) < 200:
            return 5  # 默认中等张力

        snippet = content[:500]  # 只取前 500 字，节省 token

        try:
            prompt = Prompt(
                system="你是小说节奏分析师，只输出一个 1-10 的整数，不要解释。",
                user=f"""根据以下章节开头，打分当前剧情的张力值（1=日常/轻松，10=生死对决/高潮）：

{snippet}

张力分（只输出数字）："""
            )
            config = GenerationConfig(max_tokens=5, temperature=0.1)
            result = await self.llm_service.generate(prompt, config)
            raw = result.content.strip() if hasattr(result, "content") else str(result).strip()
            score = int(''.join(filter(str.isdigit, raw[:3])))
            return max(1, min(10, score))
        except Exception:
            return 5  # 解析失败，返回默认值

    async def _stream_one_beat(self, outline, context, beat_prompt, beat, novel=None) -> str:
        """流式生成单个节拍（或整章），返回生成内容。novel 传入时在流式过程中轮询 DB 是否已停止。"""
        system = """你是一位资深网文作家，擅长写爽文。
写作要求：
1. 严格按节拍字数和聚焦点写作
2. 必须有对话和人物互动，保持人物性格一致
3. 增加感官细节：视觉、听觉、触觉、情绪
4. 节奏控制：不要一章推进太多剧情
5. 不要写章节标题"""

        user_parts = []
        if context:
            user_parts.append(context)
        user_parts.append(f"\n【本章大纲】\n{outline}")
        if beat_prompt:
            user_parts.append(f"\n{beat_prompt}")
        user_parts.append("\n\n开始撰写：")

        max_tokens = int(beat.target_words * 1.5) if beat else 3000

        prompt = Prompt(system=system, user="\n".join(user_parts))
        config = GenerationConfig(max_tokens=max_tokens, temperature=0.85)

        content = ""
        chunk_n = 0
        async for chunk in self.llm_service.stream_generate(prompt, config):
            content += chunk
            chunk_n += 1
            if novel and chunk_n % 10 == 0:
                if not self._is_still_running(novel):
                    nid = getattr(novel.novel_id, "value", novel.novel_id)
                    logger.info(f"[{nid}] 流式生成中检测到停止，截断当前输出")
                    break

        return content

    async def _upsert_chapter_content(self, novel, chapter_node, content: str, status: str):
        """最小事务：只更新章节内容，不涉及其他表"""
        from domain.novel.entities.chapter import Chapter, ChapterStatus
        from domain.novel.value_objects.novel_id import NovelId

        existing = self.chapter_repository.get_by_novel_and_number(
            NovelId(novel.novel_id.value), chapter_node.number
        )
        if existing:
            existing.update_content(content)
            existing.status = ChapterStatus(status)
            self.chapter_repository.save(existing)
        else:
            chapter = Chapter(
                id=chapter_node.id,
                novel_id=NovelId(novel.novel_id.value),
                number=chapter_node.number,
                title=chapter_node.title,
                content=content,
                outline=chapter_node.outline or "",
                status=ChapterStatus(status)
            )
            self.chapter_repository.save(chapter)

    async def _find_next_unwritten_chapter_async(self, novel):
        """找到下一个未写的章节节点"""
        novel_id = novel.novel_id.value
        all_nodes = await self.story_node_repo.get_by_novel(novel_id)
        chapter_nodes = sorted(
            [n for n in all_nodes if n.node_type.value == "chapter"],
            key=lambda n: n.number
        )

        for node in chapter_nodes:
            chapter = self.chapter_repository.get_by_novel_and_number(
                NovelId(novel_id), node.number
            )
            if not chapter or chapter.status.value != "completed":
                return node
        return None

    async def _current_act_fully_written(self, novel) -> bool:
        """检查当前幕是否已全部写完"""
        novel_id = novel.novel_id.value
        all_nodes = await self.story_node_repo.get_by_novel(novel_id)
        act_nodes = sorted(
            [n for n in all_nodes if n.node_type.value == "act"],
            key=lambda n: n.number
        )

        current_act_node = next(
            (n for n in act_nodes if n.number == novel.current_act + 1),
            None
        )
        if not current_act_node:
            return True

        act_children = self.story_node_repo.get_children_sync(current_act_node.id)
        chapter_nodes = [n for n in act_children if n.node_type.value == "chapter"]

        for node in chapter_nodes:
            chapter = self.chapter_repository.get_by_novel_and_number(
                NovelId(novel_id), node.number
            )
            if not chapter or chapter.status.value != "completed":
                return False
        return True

    async def _get_existing_chapter_content(self, novel, chapter_num) -> Optional[str]:
        """获取已存在的章节内容（用于断点续写）"""
        chapter = self.chapter_repository.get_by_novel_and_number(
            NovelId(novel.novel_id.value), chapter_num
        )
        return chapter.content if chapter else None


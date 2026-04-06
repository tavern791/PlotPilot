"""FastAPI 主应用

提供 RESTful API 接口。
"""
from pathlib import Path
import sys
import time
import logging
from datetime import datetime

# 必须在其他 aitext 模块导入前执行：将仓库根目录 `.env` 写入 os.environ
_AITEXT_ROOT = Path(__file__).resolve().parents[1]
if str(_AITEXT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AITEXT_ROOT))
try:
    from load_env import load_env

    load_env()
except Exception:
    # 无 .env 或非标准启动方式时忽略
    pass

# 配置日志（必须在导入其他模块前）
from interfaces.api.middleware.logging_config import setup_logging
import os

log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
log_file = os.getenv("LOG_FILE", "logs/aitext.log")
setup_logging(level=log_level, log_file=log_file)

logger = logging.getLogger(__name__)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Core module
from interfaces.api.v1.core import novels, chapters, scene_generation_routes

# World module
from interfaces.api.v1.world import bible, cast, knowledge, knowledge_graph_routes, worldbuilding_routes

# Blueprint module
from interfaces.api.v1.blueprint import continuous_planning_routes, beat_sheet_routes, story_structure

# Engine module
from interfaces.api.v1.engine import generation, context_intelligence, autopilot_routes, chronicles

# Audit module
from interfaces.api.v1.audit import chapter_review_routes, macro_refactor, chapter_element_routes

# Analyst module
from interfaces.api.v1.analyst import voice, narrative_state, foreshadow_ledger

# Workbench module
from interfaces.api.v1.workbench import sandbox, writer_block, monitor
from interfaces.api.stats.routers.stats import create_stats_router
from interfaces.api.stats.services.stats_service import StatsService
from interfaces.api.stats.repositories.sqlite_stats_repository_adapter import SqliteStatsRepositoryAdapter
from infrastructure.persistence.database.connection import get_database
from application.paths import DATA_DIR

# 后端版本号（每次重启递增）
BACKEND_VERSION = datetime.now().strftime("%Y%m%d-%H%M%S")
STARTUP_TIME = time.time()

logger.info("=" * 80)
logger.info(f"🚀 BACKEND STARTING - Version: {BACKEND_VERSION}")
logger.info(f"   Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
logger.info(f"   Log Level: {logging.getLevelName(log_level)}")
logger.info(f"   Log File: {log_file}")
logger.info(f"   Python: {sys.version.split()[0]}")
logger.info(f"   Working Dir: {Path.cwd()}")
logger.info("=" * 80)

# 创建 FastAPI 应用
app = FastAPI(
    title="aitext API",
    version="2.0.0",
    description="AI 小说创作平台 API"
)

@app.on_event("startup")
async def startup_event():
    """应用启动事件"""
    logger.info("📦 Loading modules and routes...")
    logger.info("✅ FastAPI application started successfully")
    logger.info(f"📊 Registered {len(app.routes)} routes")

@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭事件"""
    uptime = time.time() - STARTUP_TIME
    logger.info("=" * 80)
    logger.info(f"🛑 BACKEND SHUTTING DOWN")
    logger.info(f"   Total uptime: {uptime:.2f} seconds ({uptime/3600:.2f} hours)")
    logger.info("=" * 80)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源（开发环境）
    allow_credentials=False,  # 使用 * 时必须设为 False
    allow_methods=["*"],
    allow_headers=["*"],
)

# HTTP 访问日志由 uvicorn.access 输出（与 uvicorn 默认格式一致：IP + 请求行 + 状态码）

# Core module routes
app.include_router(novels.router, prefix="/api/v1")
app.include_router(chapters.router, prefix="/api/v1/novels")
app.include_router(scene_generation_routes.router)

# World module routes
app.include_router(bible.router, prefix="/api/v1")
app.include_router(cast.router, prefix="/api/v1")
app.include_router(knowledge.router, prefix="/api/v1")
app.include_router(knowledge_graph_routes.router)
app.include_router(worldbuilding_routes.router)

# Blueprint module routes
app.include_router(continuous_planning_routes.router)
app.include_router(beat_sheet_routes.router)
app.include_router(story_structure.router, prefix="/api/v1")

# Engine module routes
app.include_router(generation.router, prefix="/api/v1")
app.include_router(context_intelligence.router, prefix="/api/v1")
app.include_router(chronicles.router, prefix="/api/v1")
app.include_router(autopilot_routes.router, prefix="/api/v1")

# Audit module routes
app.include_router(chapter_review_routes.router)
app.include_router(macro_refactor.router, prefix="/api/v1")
app.include_router(chapter_element_routes.router)

# Analyst module routes
app.include_router(voice.router, prefix="/api/v1")
app.include_router(narrative_state.router, prefix="/api/v1")
app.include_router(foreshadow_ledger.router, prefix="/api/v1")

# Workbench module routes
app.include_router(writer_block.router, prefix="/api/v1")
app.include_router(sandbox.router, prefix="/api/v1")
app.include_router(monitor.router, prefix="/api/v1")

# 注册统计路由（使用 SQLite 适配器）
stats_repository = SqliteStatsRepositoryAdapter(get_database())
stats_service = StatsService(stats_repository)
stats_router = create_stats_router(stats_service)
app.include_router(stats_router, prefix="/api/stats", tags=["statistics"])


@app.get("/")
async def root():
    """根路径

    Returns:
        欢迎消息
    """
    return {"message": "aitext API v2.0"}


@app.get("/health")
async def health_check():
    """健康检查

    Returns:
        健康状态
    """
    uptime = time.time() - STARTUP_TIME
    return {
        "status": "healthy",
        "version": BACKEND_VERSION,
        "uptime_seconds": round(uptime, 2)
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

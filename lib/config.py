# 工具根目录
R2_DIR = "."
# R2AIBridge 服务地址
DEFAULT_BASE_URL = "http://127.0.0.1:5050"
# MCP/HTTP 请求超时(秒)
DEFAULT_TIMEOUT = 30
# AI API Base URL
DEFAULT_AI_BASE_URL = "https://api.deepseek.com/v1"
# 默认对话模型
DEFAULT_AI_MODEL = "deepseek-reasoner"
# 默认总结模型
DEFAULT_AI_SUMMARY_MODEL = "deepseek-reasoner"
# AI 请求超时(秒)
DEFAULT_AI_TIMEOUT = 45
# 单次工具结果最大保留字符数(过长截断)
MAX_TOOL_RESULT_CHARS = 5000
# 对话上下文最大消息数(过长裁剪)
MAX_CONTEXT_MESSAGES = 40
# 对话上下文最大字符预算(过长裁剪，按证据块保留)
MAX_CONTEXT_CHARS = 140000
# 配置持久化路径
CONFIG_SAVE_PATH = f"{R2_DIR}/config.json"
# AI 会话持久化路径
SESSION_SAVE_PATH = f"{R2_DIR}/session.json"
# 知识库持久化路径
KB_SAVE_PATH = f"{R2_DIR}/kb.json"
# Debug 日志（JSONL），由 config.json/CLI 的 debug 命令控制
DEBUG_LOG_PATH = f"{R2_DIR}/debug.log.jsonl"

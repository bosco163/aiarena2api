#!/usr/bin/env python3
"""
LMArena Reverse Proxy Server - 512MB VPS Optimized
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, AsyncGenerator

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

# ============ 内存优化配置 ============
MAX_CONCURRENT_REQUESTS = 3          # 限制并发，防止内存爆炸
MAX_BODY_SIZE = 10 * 1024 * 1024     # 10MB 请求体限制
LOG_QUEUE_MAXSIZE = 100              # 减小日志队列
WEBSOCKET_PING_INTERVAL = 30         # 延长 ping 间隔，减少开销

# ============ 日志配置（精简） ============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("lmarena-proxy")

# 减少第三方库日志噪音
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("fastapi").setLevel(logging.WARNING)


# ============ 数据模型 ============
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


# ============ 全局状态（精简版） ============
class ConnectionPool:
    """管理 WebSocket 连接，限制并发"""
    def __init__(self):
        self.connections: Dict[str, WebSocket] = {}
        self.active_requests: Dict[str, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self.request_queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    
    async def acquire_connection(self, timeout: float = 30.0) -> Optional[str]:
        """获取连接槽位"""
        try:
            await asyncio.wait_for(self.semaphore.acquire(), timeout=timeout)
            return str(uuid.uuid4())
        except asyncio.TimeoutError:
            return None
    
    def release_connection(self, request_id: str):
        """释放连接槽位"""
        self.semaphore.release()
        self.active_requests.pop(request_id, None)


# 全局状态
connection_pool = ConnectionPool()
stats = {
    "total_requests": 0,
    "active_requests": 0,
    "errors": 0,
    "start_time": time.time()
}


# ============ 生命周期管理 ============
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    logger.info("🚀 Server starting (512MB VPS optimized)...")
    yield
    logger.info("🛑 Server shutting down...")
    # 清理资源
    for task in connection_pool.active_requests.values():
        task.cancel()


app = FastAPI(
    title="LMArena Proxy",
    version="2.0.0-lite",
    lifespan=lifespan,
)


# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============ WebSocket 处理 ============
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """浏览器脚本连接端点"""
    await websocket.accept()
    client_id = str(uuid.uuid4())[:8]
    logger.info(f"Browser connected: {client_id}")
    
    connection_pool.connections[client_id] = websocket
    
    try:
        while True:
            # 延长超时，减少 CPU 占用
            data = await asyncio.wait_for(
                websocket.receive_json(),
                timeout=300.0
            )
            
            # 处理浏览器响应
            request_id = data.get("request_id")
            if request_id and request_id in connection_pool.active_requests:
                # 找到对应的请求并传递结果
                response_queue = getattr(connection_pool, '_response_queues', {}).get(request_id)
                if response_queue:
                    await response_queue.put(data)
                    
    except WebSocketDisconnect:
        logger.info(f"Browser disconnected: {client_id}")
    except asyncio.TimeoutError:
        logger.warning(f"Browser timeout: {client_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        connection_pool.connections.pop(client_id, None)


# ============ API 端点 ============
@app.get("/health")
async def health_check():
    """健康检查"""
    uptime = time.time() - stats["start_time"]
    return {
        "status": "healthy",
        "uptime": f"{uptime:.0f}s",
        "active_requests": stats["active_requests"],
        "browser_connected": len(connection_pool.connections) > 0,
        "memory_optimized": True
    }


@app.get("/v1/models")
async def list_models():
    """获取可用模型列表"""
    # 精简模型列表，减少内存占用
    default_models = [
        {"id": "claude-3-5-sonnet-20241022", "object": "model"},
        {"id": "gpt-4o", "object": "model"},
        {"id": "gemini-1.5-pro", "object": "model"},
        {"id": "deepseek-chat", "object": "model"},
    ]
    return {"object": "list", "data": default_models}


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """聊天补全接口"""
    if not connection_pool.connections:
        raise HTTPException(503, "No browser connected. Please open LMArena in browser with Tampermonkey script.")
    
    # 获取连接槽位
    slot_id = await connection_pool.acquire_connection()
    if not slot_id:
        raise HTTPException(429, "Server busy, too many concurrent requests")
    
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    stats["total_requests"] += 1
    stats["active_requests"] += 1
    
    # 创建响应队列
    response_queue: asyncio.Queue = asyncio.Queue()
    if not hasattr(connection_pool, '_response_queues'):
        connection_pool._response_queues = {}
    connection_pool._response_queues[request_id] = response_queue
    
    try:
        # 选择浏览器连接（简单轮询）
        browser_id = list(connection_pool.connections.keys())[0]
        browser_ws = connection_pool.connections[browser_id]
        
        # 发送请求到浏览器
        await browser_ws.send_json({
            "type": "chat_completion",
            "request_id": request_id,
            "model": request.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": request.stream,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        })
        
        if request.stream:
            return StreamingResponse(
                stream_response(request_id, response_queue),
                media_type="text/event-stream"
            )
        else:
            return await blocking_response(request_id, response_queue)
            
    except Exception as e:
        stats["errors"] += 1
        logger.error(f"Request failed: {e}")
        raise HTTPException(500, str(e))
    finally:
        stats["active_requests"] -= 1
        connection_pool.release_connection(slot_id)
        connection_pool._response_queues.pop(request_id, None)


async def stream_response(request_id: str, queue: asyncio.Queue) -> AsyncGenerator[str, None]:
    """流式响应"""
    timeout = 300.0  # 5分钟超时
    
    try:
        while True:
            data = await asyncio.wait_for(queue.get(), timeout=timeout)
            
            if data.get("type") == "error":
                yield f"data: {json.dumps({'error': data.get('message', 'Unknown error')})}\n\n"
                break
            
            if data.get("type") == "done":
                yield "data: [DONE]\n\n"
                break
            
            if data.get("type") == "chunk":
                chunk = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": data.get("model", "unknown"),
                    "choices": [{
                        "index": 0,
                        "delta": {"content": data.get("content", "")},
                        "finish_reason": None
                    }]
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                
    except asyncio.TimeoutError:
        yield f"data: {json.dumps({'error': 'Request timeout'})}\n\n"


async def blocking_response(request_id: str, queue: asyncio.Queue) -> Dict:
    """非流式响应"""
    timeout = 300.0
    contents = []
    model = "unknown"
    
    try:
        while True:
            data = await asyncio.wait_for(queue.get(), timeout=timeout)
            
            if data.get("type") == "error":
                raise HTTPException(500, data.get("message", "Unknown error"))
            
            if data.get("type") == "done":
                break
            
            if data.get("type") == "chunk":
                contents.append(data.get("content", ""))
                model = data.get("model", model)
        
        return {
            "id": request_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "".join(contents)
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": len(contents),
                "total_tokens": len(contents)
            }
        }
        
    except asyncio.TimeoutError:
        raise HTTPException(504, "Request timeout")


@app.get("/monitor")
async def monitor_page():
    """监控面板（简化版）"""
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>LMArena Proxy - Monitor</title>
        <meta charset="utf-8">
        <style>
            body {{ font-family: sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; }}
            .stat {{ background: #f0f0f0; padding: 15px; margin: 10px 0; border-radius: 8px; }}
            .ok {{ color: green; }} .warn {{ color: orange; }} .err {{ color: red; }}
        </style>
    </head>
    <body>
        <h1>📊 LMArena Proxy Monitor</h1>
        <div class="stat">
            <p>Status: <span class="{'ok' if connection_pool.connections else 'err'}">{'✅ Browser Connected' if connection_pool.connections else '❌ No Browser'}</span></p>
            <p>Active Requests: {stats['active_requests']}</p>
            <p>Total Requests: {stats['total_requests']}</p>
            <p>Errors: {stats['errors']}</p>
            <p>Uptime: {int(time.time() - stats['start_time'])}s</p>
            <p>Memory Mode: <span class="ok">512MB Optimized</span></p>
        </div>
        <h3>Connected Browsers:</h3>
        <ul>
            {''.join(f'<li>{bid}</li>' for bid in connection_pool.connections.keys()) or '<li>None</li>'}
        </ul>
        <hr>
        <p><small>API: <code>/v1/chat/completions</code> | WebSocket: <code>/ws</code></small></p>
    </body>
    </html>
    """
    return HTMLResponse(html)


from fastapi.responses import HTMLResponse


# ============ 启动入口 ============
if __name__ == "__main__":
    port = int(os.getenv("PORT", 9080))
    
    # 512MB 优化配置
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        # 关键：单 worker，禁用 reload，减少内存
        workers=1,
        reload=False,
        # 限制连接数
        limit_concurrency=MAX_CONCURRENT_REQUESTS * 2,
        limit_max_requests=1000,  # 自动重启防止内存泄漏
        # 超时设置
        timeout_keep_alive=60,
        # 日志
        access_log=False,  # 禁用访问日志，减少 I/O
    )

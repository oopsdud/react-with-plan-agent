"""
Agent System 主入口
"""
import uvicorn
from agent_system.api.routes import app

if __name__ == "__main__":
    uvicorn.run(
        "agent_system.api.routes:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )

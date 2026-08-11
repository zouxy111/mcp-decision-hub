# 任务 16 整体替换
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from hub.db.models import User
from hub.web.deps import get_current_user

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(user: User = Depends(get_current_user)):
    return HTMLResponse(f"<h1>总览</h1><p>你好，{user.username}（骨架占位，任务 16 替换）</p>")

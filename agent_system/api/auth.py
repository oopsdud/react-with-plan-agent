"""Database-backed login sessions and authenticated ownership checks."""
import hashlib
import hmac
import secrets
import time
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr
from agent_system.db.session import get_db
from agent_system.db.models import User, LoginSession
from agent_system.config.settings import get_settings
from agent_system.runtime.login_limit import LoginLimiter

login_limiter=LoginLimiter()

COOKIE='agent_session'
router=APIRouter(prefix='/auth',tags=['Authentication'])

def hash_password(password):
    salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,600000)
    return salt.hex()+':'+digest.hex()

def verify_password(password,encoded):
    salt,digest=encoded.split(':')
    actual=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),600000).hex()
    return hmac.compare_digest(actual,digest)

def seed_users(db):
    settings=get_settings()
    for i in range(1,4):
        name=f'user{i}'
        if not db.query(User).filter_by(username=name).first():
            db.add(User(username=name,password_hash=hash_password(getattr(settings,f'DEFAULT_USER{i}_PASSWORD')),is_admin=i==1))
    db.commit()

def user_view(user):return {'id':user.id,'username':user.username,'is_admin':user.is_admin}

def require_user(request:Request,db=Depends(get_db)):
    token=request.cookies.get(COOKIE)
    session=db.get(LoginSession,hashlib.sha256(token.encode()).hexdigest()) if token else None
    if not session or session.expires_at<=time.time():raise HTTPException(401,'请先登录。')
    user=db.get(User,session.user_id)
    if user is None:raise HTTPException(401,'登录已失效。')
    return user

def require_admin(user=Depends(require_user)):
    if not user.is_admin:raise HTTPException(403,'只有管理员可以修改模型服务配置。')
    return user

class LoginRequest(BaseModel):
    username:str=Field(min_length=1,max_length=64)
    password:SecretStr=Field(min_length=1,max_length=1024)

@router.post('/login')
def login(body:LoginRequest,response:Response,request:Request,db=Depends(get_db)):
    settings=get_settings()
    login_limiter.check(request.client.host if request.client else 'unknown',body.username,settings.AUTH_LOGIN_PER_IP_PER_MINUTE,settings.AUTH_LOGIN_PER_ACCOUNT_PER_MINUTE)
    user=db.query(User).filter_by(username=body.username.strip()).first()
    # Perform a KDF for unknown users too, without exposing account existence.
    encoded=user.password_hash if user else '00'*16+':'+'00'*32
    if not verify_password(body.password.get_secret_value(),encoded) or user is None:
        raise HTTPException(401,'用户名或密码错误。')
    previous=request.cookies.get(COOKIE)
    if previous:db.query(LoginSession).filter_by(token_hash=hashlib.sha256(previous.encode()).hexdigest()).delete()
    db.query(LoginSession).filter(LoginSession.expires_at<=time.time()).delete()
    token=secrets.token_urlsafe(32)
    settings=get_settings()
    db.add(LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(),user_id=user.id,expires_at=time.time()+settings.SESSION_TTL_SECONDS))
    db.commit()
    response.set_cookie(COOKIE,token,httponly=True,samesite='strict',secure=settings.COOKIE_SECURE,max_age=settings.SESSION_TTL_SECONDS,path='/')
    response.headers['Cache-Control']='no-store'
    return user_view(user)

@router.get('/me')
def me(response:Response,user=Depends(require_user)):
    response.headers['Cache-Control']='no-store'
    return user_view(user)

@router.post('/logout')
def logout(request:Request,response:Response,db=Depends(get_db)):
    token=request.cookies.get(COOKIE)
    if token:db.query(LoginSession).filter_by(token_hash=hashlib.sha256(token.encode()).hexdigest()).delete()
    db.commit()
    response.delete_cookie(COOKIE,path='/',samesite='strict',secure=get_settings().COOKIE_SECURE,httponly=True)
    return {'success':True}

@router.post('/register',status_code=403)
def register():
    raise HTTPException(403,'注册暂未开放，请使用预置账号。')

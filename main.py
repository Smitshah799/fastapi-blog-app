from fastapi import FastAPI, Request, Form, Depends, status, HTTPException, Response
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
import motor.motor_asyncio
import socketio
import os
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from bson import ObjectId
from fastapi.middleware.gzip import GZipMiddleware
from loguru import logger

# Load environment variables
load_dotenv()

logger.add("logs.log", rotation="1 week", retention="10 days", level="INFO")

# Environment settings (securely stored in .env file)
SECRET_KEY = os.getenv("SECRET_KEY", "default-secret-key")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://localhost:27017")

# MongoDB setup
client = motor.motor_asyncio.AsyncIOMotorClient(
    MONGODB_URL, maxPoolSize=10, minPoolSize=5
)

db = client.blogdb
users_collection = db.users
posts_collection = db.posts

# FastAPI setup
fastapi_app = FastAPI()
templates = Jinja2Templates(directory="templates")
fastapi_app.mount("/static", StaticFiles(directory="static"), name="static")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")
fastapi_app.add_middleware(GZipMiddleware, minimum_size=1000)

# Socket.IO integration
sio = socketio.AsyncServer(async_mode='asgi')
app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app)

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str):
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str):
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        user = await users_collection.find_one({"username": username})
        if user is None:
            raise HTTPException(status_code=401, detail="User not found")
        return user
    except JWTError:
        raise HTTPException(status_code=401, detail="Could not validate credentials")

@fastapi_app.get("/", response_class=HTMLResponse)
async def splash_screen(request: Request):
    return templates.TemplateResponse("splash.html", {"request": request})

@fastapi_app.get("/home", response_class=HTMLResponse)
async def home(request: Request):
    blogs_cursor = posts_collection.find().sort("_id", -1)
    blogs = await blogs_cursor.to_list(length=100)
    token = request.cookies.get("access_token")
    user = None
    if token:
        try:
            user = await get_current_user(token)
        except:
            pass
    return templates.TemplateResponse("index.html", {"request": request, "user": user, "blogs": blogs})

@fastapi_app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@fastapi_app.post("/token")
async def login_token(request: Request, response: Response, form_data: OAuth2PasswordRequestForm = Depends()):
    user = await users_collection.find_one({"username": form_data.username})
    if not user or not verify_password(form_data.password, user.get("password", "")):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials. Please try again."}, status_code=status.HTTP_401_UNAUTHORIZED)
    access_token = create_access_token(data={"sub": user["username"]})
    response = RedirectResponse(url="/home", status_code=status.HTTP_302_FOUND)
    response.set_cookie("access_token", access_token, httponly=True, secure=True, samesite="Lax")
    return response

@fastapi_app.get("/signup", response_class=HTMLResponse)
async def signup_get(request: Request):
    return templates.TemplateResponse("signup.html", {"request": request})

@fastapi_app.post("/signup")
async def signup_post(request: Request, username: str = Form(...), password: str = Form(...)):
    existing = await users_collection.find_one({"username": username})
    if existing:
        return templates.TemplateResponse("signup.html", {"request": request, "error": "Username already exists"})
    hashed_pw = hash_password(password)
    await users_collection.insert_one({"username": username, "password": hashed_pw})
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

@fastapi_app.get("/logout")
async def logout(request: Request, response: Response):
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie("access_token")
    return response

@fastapi_app.get("/create", response_class=HTMLResponse)
async def create_blog_get(request: Request):
    token = request.cookies.get("access_token")
    user = await get_current_user(token)
    return templates.TemplateResponse("create_blog.html", {"request": request, "user": user})

@fastapi_app.post("/create")
async def create_blog_post(request: Request, title: str = Form(...), content: str = Form(...)):
    token = request.cookies.get("access_token")
    user = await get_current_user(token)
    blog = {"author": user["username"], "title": title, "content": content, "created_at": datetime.utcnow()}
    await posts_collection.insert_one(blog)
    await sio.emit("new_blog", blog)
    return JSONResponse(status_code=201, content={"message": "Blog post created successfully"})

@fastapi_app.get("/manage", response_class=HTMLResponse)
async def manage_posts(request: Request):
    token = request.cookies.get("access_token")
    user = await get_current_user(token)
    user_posts_cursor = posts_collection.find({"author": user["username"]})
    user_posts = await user_posts_cursor.to_list(length=100)
    return templates.TemplateResponse("manage_posts.html", {"request": request, "user": user, "posts": user_posts})

@fastapi_app.post("/delete_post/{post_id}")
async def delete_post(request: Request, post_id: str):
    token = request.cookies.get("access_token")
    user = await get_current_user(token)
    post = await posts_collection.find_one({"_id": ObjectId(post_id), "author": user["username"]})
    if not post:
        return JSONResponse(status_code=403, content={"message": "Unauthorized to delete this post"})
    await posts_collection.delete_one({"_id": ObjectId(post_id)})
    return JSONResponse(status_code=200, content={"message": "Blog post deleted successfully"})


# Add breadcrumb support for mobile navigation
@fastapi_app.get("/navbar", response_class=HTMLResponse)
async def navbar(request: Request):
    token = request.cookies.get("access_token")
    user = None
    if token:
        try:
            user = await get_current_user(token)
        except:
            pass
    return templates.TemplateResponse("nav.html", {"request": request, "user": user})

# Optimize script loading for performance
@fastapi_app.get("/scripts", response_class=HTMLResponse)
async def load_scripts(request: Request):
    return templates.TemplateResponse("scripts.html", {"request": request})


@sio.event
async def connect(sid, environ):
    print("Client connected:", sid)

@sio.event
async def disconnect(sid):
    print("Client disconnected:", sid)

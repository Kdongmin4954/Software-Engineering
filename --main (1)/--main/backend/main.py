import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from postgrest.exceptions import APIError
from pydantic import BaseModel, Field
from supabase import Client, create_client

# 1. 환경변수 로드 및 Supabase 연결
load_dotenv(Path(__file__).with_name(".env"))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
CHAT_SESSION_SECRET = os.getenv("CHAT_SESSION_SECRET") or secrets.token_hex(32)
logger = logging.getLogger(__name__)

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(".env 파일에 SUPABASE_URL과 SUPABASE_KEY가 설정되어 있지 않습니다.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="동네마켓 정식 백엔드 API (10-Table ERD)")

# 프론트엔드(HTML/JS) 통신 허용 (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 메인 주소(http://127.0.0.1:8000) 접속 시 프론트엔드 화면 띄우기
@app.get("/", include_in_schema=False)
def serve_frontend():
    frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")
    if not os.path.exists(frontend_path):
        raise HTTPException(
            status_code=404,
            detail=f"index.html 파일을 찾을 수 없습니다. 경로 확인: {os.path.abspath(frontend_path)}",
        )
    html = Path(frontend_path).read_text(encoding="utf-8")
    return HTMLResponse(html.replace('content="static-preview"', 'content="fastapi"', 1))


# ==========================================
# 유틸리티 & 공통 조회 함수
# ==========================================
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def sign_chat_session(payload: str) -> str:
    return hmac.new(CHAT_SESSION_SECRET.encode(), f"chat-session:{payload}".encode(), hashlib.sha256).hexdigest()


def issue_chat_session(user_id: int) -> str:
    payload = f"{user_id}.{int(time.time()) + 7 * 24 * 60 * 60}"
    return f"{payload}.{sign_chat_session(payload)}"


def chat_session_user(token: str) -> int:
    try:
        user_id, expires, signature = token.split(".")
        if int(expires) > time.time() and hmac.compare_digest(
            signature, sign_chat_session(f"{user_id}.{expires}")
        ):
            return int(user_id)
    except ValueError:
        pass
    raise HTTPException(status_code=401, detail="채팅 기능을 사용하려면 다시 로그인해 주세요.")


def get_user_profile(user_id: int) -> dict:
    user_res = (
        supabase.table("users")
        .select("user_id, nickname, phone_num")
        .eq("user_id", user_id)
        .execute()
    )
    if not user_res.data:
        raise HTTPException(status_code=404, detail="존재하지 않는 회원입니다.")

    user = user_res.data[0]

    # 가장 최근 동네 인증 내역 조회
    ver_res = (
        supabase.table("user_neighborhood")
        .select("verification_id, verified_at, verification_status, neighborhood_id")
        .eq("user_id", user_id)
        .order("verified_at", desc=True)
        .limit(1)
        .execute()
    )

    neighborhood_id = None
    neighborhood_name = None
    region_info = None
    is_verified = False

    if ver_res.data:
        latest = ver_res.data[0]
        neighborhood_id = latest["neighborhood_id"]
        is_verified = latest["verification_status"] == "인증완료"

        nb_res = (
            supabase.table("neighborhood")
            .select("neighborhood_name, region_info")
            .eq("neighborhood_id", neighborhood_id)
            .execute()
        )
        if nb_res.data:
            neighborhood_name = nb_res.data[0]["neighborhood_name"]
            region_info = nb_res.data[0]["region_info"]

    return {
        "user_id": user["user_id"],
        "nickname": user["nickname"],
        "phone_num": user["phone_num"],
        "neighborhood_id": neighborhood_id,
        "neighborhood_name": neighborhood_name,
        "region_info": region_info,
        "is_verified": is_verified,
    }


def enrich_posts(posts: List[dict], current_user_id: Optional[int] = None) -> List[dict]:
    if not posts:
        return []

    post_ids = [p["post_id"] for p in posts]
    user_ids = list({p["user_id"] for p in posts})
    nb_ids = list({p["neighborhood_id"] for p in posts})

    # 작성자 닉네임 매핑
    users_res = (
        supabase.table("users")
        .select("user_id, nickname")
        .in_("user_id", user_ids)
        .execute()
    )
    user_map = {u["user_id"]: u["nickname"] for u in users_res.data}

    # 동네 정보 매핑
    nb_res = (
        supabase.table("neighborhood")
        .select("neighborhood_id, neighborhood_name, region_info")
        .in_("neighborhood_id", nb_ids)
        .execute()
    )
    nb_map = {n["neighborhood_id"]: n for n in nb_res.data}

    # 상품 이미지 매핑
    img_res = (
        supabase.table("product_image")
        .select("post_id, image_url")
        .in_("post_id", post_ids)
        .order("image_id")
        .execute()
    )
    img_map: Dict[int, List[str]] = {}
    for img in img_res.data:
        img_map.setdefault(img["post_id"], []).append(img["image_url"])

    # 관심목록 수 및 현재 사용자 찜 여부
    wish_res = (
        supabase.table("wishlist")
        .select("post_id, user_id")
        .in_("post_id", post_ids)
        .execute()
    )
    wish_count_map: Dict[int, int] = {}
    wished_post_ids = set()
    for w in wish_res.data:
        wish_count_map[w["post_id"]] = wish_count_map.get(w["post_id"], 0) + 1
        if current_user_id and w["user_id"] == current_user_id:
            wished_post_ids.add(w["post_id"])

    # 채팅방 수
    chat_res = (
        supabase.table("chatroom")
        .select("post_id")
        .in_("post_id", post_ids)
        .execute()
    )
    chat_count_map: Dict[int, int] = {}
    for c in chat_res.data:
        chat_count_map[c["post_id"]] = chat_count_map.get(c["post_id"], 0) + 1

    enriched = []
    for p in posts:
        pid = p["post_id"]
        nb = nb_map.get(p["neighborhood_id"], {})
        images = img_map.get(pid, [])
        enriched.append(
            {
                **p,
                "seller_nickname": user_map.get(p["user_id"], "알 수 없음"),
                "neighborhood_name": nb.get("neighborhood_name", ""),
                "region_info": nb.get("region_info", ""),
                "images": images,
                "thumbnail": images[0] if images else None,
                "wish_count": wish_count_map.get(pid, 0),
                "chat_count": chat_count_map.get(pid, 0),
                "is_wished": pid in wished_post_ids,
            }
        )
    return enriched


# ==========================================
# Pydantic 요청 모델
# ==========================================
class SignupRequest(BaseModel):
    phone_num: str = Field(..., example="010-1234-5678")
    password: str = Field(..., example="1234")
    nickname: str = Field(..., example="연남이")


class LoginRequest(BaseModel):
    phone_num: str = Field(..., example="010-1234-5678")
    password: str = Field(..., example="1234")


class VerifyNeighborhoodRequest(BaseModel):
    user_id: int
    neighborhood_id: int


class PostCreateRequest(BaseModel):
    user_id: int
    neighborhood_id: int
    post_title: str
    price: int = Field(..., ge=0)
    description: str = ""
    images: List[str] = []


class PostStatusUpdateRequest(BaseModel):
    status: str  # 판매중, 예약중, 거래완료


class WishlistToggleRequest(BaseModel):
    user_id: int
    post_id: int


class ReservationCreateRequest(BaseModel):
    post_id: int
    user_id: int
    reservation_at: Optional[str] = None


class ReservationStatusUpdateRequest(BaseModel):
    reservation_status: str  # 예약대기, 예약확정, 예약취소, 거래완료


class ChatroomCreateRequest(BaseModel):
    post_id: int
    buyer_id: int


class ChatMessageCreateRequest(BaseModel):
    user_id: int
    content: str


# ==========================================
# WebSocket 실시간 채팅 매니저
# ==========================================
class ConnectionManager:
    def __init__(self):
        self.active_rooms: Dict[int, List[WebSocket]] = {}

    async def connect(self, chatroom_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active_rooms.setdefault(chatroom_id, []).append(websocket)

    def disconnect(self, chatroom_id: int, websocket: WebSocket):
        room = self.active_rooms.get(chatroom_id, [])
        if websocket in room:
            room.remove(websocket)
        if not room and chatroom_id in self.active_rooms:
            del self.active_rooms[chatroom_id]

    async def broadcast(self, chatroom_id: int, payload: dict):
        dead_sockets = []
        for ws in self.active_rooms.get(chatroom_id, [])[:]:
            try:
                await ws.send_json(payload)
            except Exception:
                dead_sockets.append(ws)
        for ws in dead_sockets:
            self.disconnect(chatroom_id, ws)


manager = ConnectionManager()
chat_updates = ConnectionManager()


def room_members(chatroom_id: int) -> List[int]:
    rows = (
        supabase.table("chat_participant").select("user_id")
        .eq("chatroom_id", chatroom_id).execute().data
    )
    return [row["user_id"] for row in rows]


async def notify_chat_users(user_ids: List[int], payload: dict):
    for user_id in set(user_ids):
        await chat_updates.broadcast(user_id, payload)


async def notify_reservation_change(reservation: dict):
    try:
        seller = await run_in_threadpool(
            lambda: supabase.table("product_post").select("user_id")
            .eq("post_id", reservation["post_id"]).execute().data
        )
    except Exception:
        logger.exception("Could not notify the seller about a saved reservation")
        seller = []
    await notify_chat_users(
        [reservation["user_id"], *(row["user_id"] for row in seller)],
        {"type": "reservation", "post_id": reservation["post_id"], "buyer_id": reservation["user_id"]},
    )


# ==========================================
# 1. 회원 (User) API
# ==========================================
@app.post("/api/signup", tags=["회원"])
def signup(req: SignupRequest):
    existing = (
        supabase.table("users")
        .select("user_id")
        .eq("phone_num", req.phone_num)
        .execute()
    )
    if existing.data:
        raise HTTPException(status_code=400, detail="이미 가입된 전화번호입니다.")

    res = (
        supabase.table("users")
        .insert(
            {
                "phone_num": req.phone_num,
                "password": hash_password(req.password),
                "nickname": req.nickname,
            }
        )
        .execute()
    )
    new_user = res.data[0]
    user_id = new_user["user_id"]
    return {"message": "회원가입 성공", "user": get_user_profile(user_id), "chat_session": issue_chat_session(user_id)}


@app.post("/api/login", tags=["회원"])
def login(req: LoginRequest):
    res = (
        supabase.table("users")
        .select("user_id, password")
        .eq("phone_num", req.phone_num)
        .execute()
    )
    if not res.data or res.data[0]["password"] != hash_password(req.password):
        raise HTTPException(status_code=401, detail="전화번호 또는 비밀번호가 올바르지 않습니다.")

    user_id = res.data[0]["user_id"]
    return {"message": "로그인 성공", "user": get_user_profile(user_id), "chat_session": issue_chat_session(user_id)}


@app.get("/api/users/{user_id}", tags=["회원"])
def get_user(user_id: int):
    return get_user_profile(user_id)


# ==========================================
# 2. 동네 & 동네 인증 (Neighborhood / User_Neighborhood) API
# ==========================================
@app.get("/api/neighborhoods", tags=["동네인증"])
def get_neighborhoods():
    res = supabase.table("neighborhood").select("*").order("neighborhood_id").execute()
    return res.data


@app.post("/api/verify-neighborhood", tags=["동네인증"])
def verify_neighborhood(req: VerifyNeighborhoodRequest):
    # 회원 및 동네 유효성 검증
    get_user_profile(req.user_id)
    nb_res = (
        supabase.table("neighborhood")
        .select("*")
        .eq("neighborhood_id", req.neighborhood_id)
        .execute()
    )
    if not nb_res.data:
        raise HTTPException(status_code=404, detail="존재하지 않는 동네입니다.")

    ver_res = (
        supabase.table("user_neighborhood")
        .insert(
            {
                "user_id": req.user_id,
                "neighborhood_id": req.neighborhood_id,
                "verification_status": "인증완료",
            }
        )
        .execute()
    )
    return {
        "message": f"{nb_res.data[0]['neighborhood_name']} 동네 인증 완료",
        "verification": ver_res.data[0],
        "user": get_user_profile(req.user_id),
    }


# ==========================================
# 3. 판매게시글 & 상품이미지 (ProductPost / ProductImage) API
# ==========================================
@app.get("/api/posts", tags=["게시글"])
def get_posts(
    neighborhood_id: Optional[int] = Query(default=None),
    status: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    seller_id: Optional[int] = Query(default=None),
    user_id: Optional[int] = Query(default=None),
):
    query = supabase.table("product_post").select("*").order("post_created", desc=True)

    if neighborhood_id is not None:
        query = query.eq("neighborhood_id", neighborhood_id)
    if status and status != "전체":
        query = query.eq("status", status)
    if seller_id is not None:
        query = query.eq("user_id", seller_id)

    rows = query.execute().data

    if q:
        keyword = q.strip().lower()
        rows = [
            p
            for p in rows
            if keyword in (p.get("post_title") or "").lower()
            or keyword in (p.get("description") or "").lower()
        ]

    return enrich_posts(rows, current_user_id=user_id)


@app.post("/api/posts", tags=["게시글"])
def create_post(req: PostCreateRequest):
    post_res = (
        supabase.table("product_post")
        .insert(
            {
                "post_title": req.post_title,
                "description": req.description,
                "price": req.price,
                "status": "판매중",
                "user_id": req.user_id,
                "neighborhood_id": req.neighborhood_id,
            }
        )
        .execute()
    )
    created = post_res.data[0]
    post_id = created["post_id"]

    if req.images:
        img_rows = [{"image_url": url, "post_id": post_id} for url in req.images if url]
        if img_rows:
            supabase.table("product_image").insert(img_rows).execute()

    return {
        "message": "게시글 등록 완료",
        "post": enrich_posts([created], current_user_id=req.user_id)[0],
    }


@app.get("/api/posts/{post_id}", tags=["게시글"])
def get_post_detail(post_id: int, user_id: Optional[int] = Query(default=None)):
    res = supabase.table("product_post").select("*").eq("post_id", post_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="게시글을 찾을 수 없습니다.")
    return enrich_posts(res.data, current_user_id=user_id)[0]


@app.patch("/api/posts/{post_id}/status", tags=["게시글"])
def update_post_status(post_id: int, req: PostStatusUpdateRequest):
    if req.status not in ("판매중", "예약중", "거래완료"):
        raise HTTPException(status_code=400, detail="유효하지 않은 거래 상태입니다.")

    res = (
        supabase.table("product_post")
        .update({"status": req.status})
        .eq("post_id", post_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="게시글을 찾을 수 없습니다.")
    return {"message": "상태 변경 완료", "post": res.data[0]}

@app.delete("/api/posts/{post_id}", tags=["게시글"])
def delete_post(post_id: int, user_id: int = Query(...)):
    res = (
        supabase.table("product_post")
        .select("post_id, user_id")
        .eq("post_id", post_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="게시글을 찾을 수 없습니다.")

    if res.data[0]["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="본인이 작성한 게시글만 삭제할 수 있습니다.")

    supabase.table("product_post").delete().eq("post_id", post_id).execute()
    return {"message": "게시글 삭제 완료", "post_id": post_id}


# ==========================================
# 4. 관심목록 (Wishlist) API
# ==========================================
@app.get("/api/wishlist/{user_id}", tags=["관심목록"])
def get_wishlist(user_id: int):
    wish_res = (
        supabase.table("wishlist")
        .select("post_id, created_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    if not wish_res.data:
        return []

    ordered_post_ids = [w["post_id"] for w in wish_res.data]
    posts_res = (
        supabase.table("product_post")
        .select("*")
        .in_("post_id", ordered_post_ids)
        .execute()
    )
    enriched = enrich_posts(posts_res.data, current_user_id=user_id)
    post_map = {p["post_id"]: p for p in enriched}
    return [post_map[pid] for pid in ordered_post_ids if pid in post_map]


@app.post("/api/wishlist/toggle", tags=["관심목록"])
def toggle_wishlist(req: WishlistToggleRequest):
    existing = (
        supabase.table("wishlist")
        .select("wishlist_id")
        .eq("user_id", req.user_id)
        .eq("post_id", req.post_id)
        .execute()
    )
    if existing.data:
        supabase.table("wishlist").delete().eq(
            "wishlist_id", existing.data[0]["wishlist_id"]
        ).execute()
        wished = False
    else:
        supabase.table("wishlist").insert(
            {"user_id": req.user_id, "post_id": req.post_id}
        ).execute()
        wished = True

    count_res = (
        supabase.table("wishlist")
        .select("wishlist_id")
        .eq("post_id", req.post_id)
        .execute()
    )
    return {"wished": wished, "wish_count": len(count_res.data)}


# ==========================================
# 5. 예약정보 (ReservationInfo) API
# ==========================================
@app.post("/api/reservations", tags=["예약"])
async def create_reservation_endpoint(req: ReservationCreateRequest):
    result = await run_in_threadpool(create_reservation, req)
    await notify_reservation_change(result["reservation"])
    return result


def create_reservation(req: ReservationCreateRequest):
    payload = {
        "post_id": req.post_id,
        "user_id": req.user_id,
        "reservation_status": "예약대기",
    }
    if req.reservation_at:
        payload["reservation_at"] = req.reservation_at

    res = supabase.table("reservation_info").insert(payload).execute()
    return {"message": "예약 제안 완료", "reservation": res.data[0]}


@app.patch("/api/reservations/{reservation_id}", tags=["예약"])
async def update_reservation_endpoint(reservation_id: int, req: ReservationStatusUpdateRequest):
    result = await run_in_threadpool(update_reservation, reservation_id, req)
    await notify_reservation_change(result["reservation"])
    return result


def update_reservation(reservation_id: int, req: ReservationStatusUpdateRequest):
    if req.reservation_status not in ("예약대기", "예약확정", "예약취소", "거래완료"):
        raise HTTPException(status_code=400, detail="유효하지 않은 예약 상태입니다.")

    update_data = {"reservation_status": req.reservation_status}
    if req.reservation_status == "예약취소":
        update_data["canceled_at"] = datetime.now(timezone.utc).isoformat()

    res = (
        supabase.table("reservation_info")
        .update(update_data)
        .eq("reservation_id", reservation_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="예약 정보를 찾을 수 없습니다.")

    reservation = res.data[0]
    post_id = reservation["post_id"]

    # 예약 상태에 맞춰 게시글 거래 상태 자동 동기화
    status_sync_map = {
        "예약확정": "예약중",
        "예약취소": "판매중",
        "거래완료": "거래완료",
    }
    new_post_status = status_sync_map.get(req.reservation_status)
    if new_post_status:
        supabase.table("product_post").update({"status": new_post_status}).eq(
            "post_id", post_id
        ).execute()

    return {
        "message": f"{req.reservation_status} 처리 완료",
        "reservation": reservation,
        "post_status": new_post_status,
    }


@app.get("/api/reservations/{user_id}", tags=["예약"])
def get_user_reservations(user_id: int):
    # 내가 구매자로 예약한 내역 + 내가 올린 글에 들어온 예약 내역 모두 조회
    my_posts = (
        supabase.table("product_post")
        .select("post_id")
        .eq("user_id", user_id)
        .execute()
        .data
    )
    my_post_ids = [p["post_id"] for p in my_posts]

    buyer_res = (
        supabase.table("reservation_info")
        .select("*")
        .eq("user_id", user_id)
        .order("reservation_at", desc=True)
        .execute()
        .data
    )
    seller_res = (
        supabase.table("reservation_info")
        .select("*")
        .in_("post_id", my_post_ids)
        .order("reservation_at", desc=True)
        .execute()
        .data
        if my_post_ids
        else []
    )

    merged_map = {r["reservation_id"]: r for r in buyer_res + seller_res}
    reservations = sorted(
        merged_map.values(), key=lambda x: x["reservation_at"], reverse=True
    )
    if not reservations:
        return []

    post_ids = list({r["post_id"] for r in reservations})
    posts = (
        supabase.table("product_post")
        .select("*")
        .in_("post_id", post_ids)
        .execute()
        .data
    )
    enriched_posts = {
        p["post_id"]: p for p in enrich_posts(posts, current_user_id=user_id)
    }

    buyer_ids = list({r["user_id"] for r in reservations})
    buyers = (
        supabase.table("users")
        .select("user_id, nickname")
        .in_("user_id", buyer_ids)
        .execute()
        .data
    )
    buyer_map = {b["user_id"]: b["nickname"] for b in buyers}

    result = []
    for r in reservations:
        result.append(
            {
                **r,
                "buyer_nickname": buyer_map.get(r["user_id"], "알 수 없음"),
                "post": enriched_posts.get(r["post_id"]),
            }
        )
    return result


# ==========================================
# 6. 채팅방 / 참여 / 메시지 (Chatroom / ChatParticipant / ChatMessage) API
# ==========================================
@app.post("/api/chatrooms", tags=["채팅"])
def get_or_create_chatroom(req: ChatroomCreateRequest):
    post_res = (
        supabase.table("product_post")
        .select("post_id, user_id")
        .eq("post_id", req.post_id)
        .execute()
    )
    if not post_res.data:
        raise HTTPException(status_code=404, detail="게시글을 찾을 수 없습니다.")

    seller_id = post_res.data[0]["user_id"]
    if seller_id == req.buyer_id:
        raise HTTPException(status_code=400, detail="본인의 게시글에는 채팅방을 만들 수 없습니다.")

    # 해당 게시글에 이미 참여 중인 채팅방이 있는지 확인
    rooms = (
        supabase.table("chatroom")
        .select("chatroom_id")
        .eq("post_id", req.post_id)
        .execute()
        .data
    )
    room_ids = [r["chatroom_id"] for r in rooms]
    if room_ids:
        existing_part = (
            supabase.table("chat_participant")
            .select("chatroom_id")
            .eq("user_id", req.buyer_id)
            .in_("chatroom_id", room_ids)
            .execute()
            .data
        )
        if existing_part:
            return {
                "message": "기존 채팅방 입장",
                "chatroom_id": existing_part[0]["chatroom_id"],
            }

    # 새 채팅방 생성 및 판매자/구매자 참여 등록
    new_room = (
        supabase.table("chatroom").insert({"post_id": req.post_id}).execute().data[0]
    )
    chatroom_id = new_room["chatroom_id"]

    supabase.table("chat_participant").insert(
        [
            {"user_id": seller_id, "chatroom_id": chatroom_id},
            {"user_id": req.buyer_id, "chatroom_id": chatroom_id},
        ]
    ).execute()

    return {"message": "새 채팅방 생성 완료", "chatroom_id": chatroom_id}


@app.delete("/api/chatrooms/{chatroom_id}", tags=["채팅"])
async def delete_chatroom(chatroom_id: int, authorization: str = Header(default="")):
    user_id = chat_session_user(authorization.removeprefix("Bearer "))
    members = await run_in_threadpool(room_members, chatroom_id)
    if user_id not in members:
        raise HTTPException(status_code=404, detail="삭제할 채팅방을 찾을 수 없습니다.")

    try:
        deleted = await run_in_threadpool(
            lambda: supabase.table("chatroom").delete().eq("chatroom_id", chatroom_id).execute().data
        )
    except APIError as error:
        if error.code == "23503":
            raise HTTPException(status_code=409, detail="채팅방 삭제를 위한 데이터베이스 설정이 필요해요.") from error
        raise
    if not deleted:
        raise HTTPException(status_code=404, detail="삭제할 채팅방을 찾을 수 없습니다.")
    payload = {"type": "room_deleted", "chatroom_id": chatroom_id, "deleted_by": user_id}
    await notify_chat_users(members, payload)
    await manager.broadcast(chatroom_id, payload)
    return {"message": "채팅방 삭제 완료"}


@app.get("/api/chatrooms/{user_id}", tags=["채팅"])
def get_chatrooms(user_id: int):
    part_res = (
        supabase.table("chat_participant")
        .select("chatroom_id")
        .eq("user_id", user_id)
        .execute()
        .data
    )
    if not part_res:
        return []

    room_ids = [p["chatroom_id"] for p in part_res]
    rooms = (
        supabase.table("chatroom")
        .select("*")
        .in_("chatroom_id", room_ids)
        .execute()
        .data
    )
    if not rooms:
        return []

    post_ids = list({r["post_id"] for r in rooms})
    posts = (
        supabase.table("product_post")
        .select("*")
        .in_("post_id", post_ids)
        .execute()
        .data
    )
    post_map = {p["post_id"]: p for p in enrich_posts(posts, current_user_id=user_id)}

    all_parts = (
        supabase.table("chat_participant")
        .select("chatroom_id, user_id")
        .in_("chatroom_id", room_ids)
        .execute()
        .data
    )
    other_user_ids = list({p["user_id"] for p in all_parts if p["user_id"] != user_id})
    users = (
        supabase.table("users")
        .select("user_id, nickname")
        .in_("user_id", other_user_ids)
        .execute()
        .data
        if other_user_ids
        else []
    )
    user_map = {u["user_id"]: u["nickname"] for u in users}

    messages = (
        supabase.table("chat_message")
        .select("*")
        .in_("chatroom_id", room_ids)
        .order("sent_at", desc=True)
        .order("message_id", desc=True)
        .execute()
        .data
    )
    last_msg_map: Dict[int, dict] = {}
    last_received_map: Dict[int, int] = {}
    for m in messages:
        cid = m["chatroom_id"]
        last_msg_map.setdefault(cid, m)
        if m["user_id"] != user_id:
            last_received_map[cid] = max(last_received_map.get(cid, 0), m["message_id"])

    reservations = (
        supabase.table("reservation_info")
        .select("*")
        .in_("post_id", post_ids)
        .order("reservation_id", desc=True)
        .execute()
        .data
    )
    latest_res_map = {}
    for r in reservations:
        latest_res_map.setdefault((r["post_id"], r["user_id"]), r)

    result = []
    for room in rooms:
        cid = room["chatroom_id"]
        partner_id = next(
            (p["user_id"] for p in all_parts if p["chatroom_id"] == cid and p["user_id"] != user_id),
            None,
        )
        last_msg = last_msg_map.get(cid)
        post = post_map.get(room["post_id"])
        buyer_id = partner_id if post and post["user_id"] == user_id else user_id
        latest_res = latest_res_map.get((room["post_id"], buyer_id))

        result.append(
            {
                "chatroom_id": cid,
                "post_id": room["post_id"],
                "chat_created": room["chat_created"],
                "partner_id": partner_id,
                "partner_nickname": user_map.get(partner_id, "알 수 없음"),
                "post": post,
                "last_message": last_msg["content"] if last_msg else "대화를 시작해 보세요.",
                "last_sent_at": last_msg["sent_at"] if last_msg else room["chat_created"],
                "last_received_message_id": last_received_map.get(cid, 0),
                "latest_reservation": latest_res,
            }
        )

    result.sort(key=lambda x: x["last_sent_at"], reverse=True)
    return result


@app.get("/api/chatrooms/{chatroom_id}/messages", tags=["채팅"])
def get_chat_messages(chatroom_id: int):
    room_res = (
        supabase.table("chatroom")
        .select("*")
        .eq("chatroom_id", chatroom_id)
        .execute()
    )
    if not room_res.data:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")

    room = room_res.data[0]
    post_res = (
        supabase.table("product_post")
        .select("*")
        .eq("post_id", room["post_id"])
        .execute()
    )
    post = enrich_posts(post_res.data)[0] if post_res.data else None

    msgs = (
        supabase.table("chat_message")
        .select("*")
        .eq("chatroom_id", chatroom_id)
        .order("sent_at", desc=True)
        .order("message_id", desc=True)
        .execute()
        .data
    )
    sender_ids = list({m["user_id"] for m in msgs})
    users = (
        supabase.table("users")
        .select("user_id, nickname")
        .in_("user_id", sender_ids)
        .execute()
        .data
        if sender_ids
        else []
    )
    user_map = {u["user_id"]: u["nickname"] for u in users}

    enriched_msgs = [
        {**m, "sender_nickname": user_map.get(m["user_id"], "알 수 없음")}
        for m in reversed(msgs)
    ]

    members = room_members(chatroom_id)
    buyer_id = next((member for member in members if post and member != post["user_id"]), None)
    latest_res = (
        supabase.table("reservation_info")
        .select("*")
        .eq("post_id", room["post_id"])
        .eq("user_id", buyer_id)
        .order("reservation_id", desc=True)
        .limit(1)
        .execute()
        .data if buyer_id is not None else []
    )

    return {
        "chatroom_id": chatroom_id,
        "post": post,
        "buyer_id": buyer_id,
        "latest_reservation": latest_res[0] if latest_res else None,
        "messages": enriched_msgs,
    }


@app.post("/api/chatrooms/{chatroom_id}/messages", tags=["채팅"])
async def send_chat_message(chatroom_id: int, req: ChatMessageCreateRequest):
    members = await run_in_threadpool(room_members, chatroom_id)
    if req.user_id not in members:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")
    payload = await run_in_threadpool(save_chat_message, chatroom_id, req)
    await manager.broadcast(chatroom_id, payload)
    await notify_chat_users(members, {"type": "message", "chatroom_id": chatroom_id})
    return payload


def save_chat_message(chatroom_id: int, req: ChatMessageCreateRequest):
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="메시지를 입력해 주세요.")
    saved = (
        supabase.table("chat_message")
        .insert(
            {
                "chatroom_id": chatroom_id,
                "user_id": req.user_id,
                "content": content,
            }
        )
        .execute()
        .data[0]
    )
    user_res = (
        supabase.table("users")
        .select("nickname")
        .eq("user_id", req.user_id)
        .execute()
    )
    return {
        **saved,
        "sender_nickname": user_res.data[0]["nickname"] if user_res.data else "알 수 없음",
    }


@app.websocket("/ws/chat-updates")
async def websocket_chat_updates(websocket: WebSocket):
    await websocket.accept()
    try:
        token = await asyncio.wait_for(websocket.receive_text(), timeout=5)
        user_id = chat_session_user(token)
    except (HTTPException, asyncio.TimeoutError):
        await websocket.close(code=1008)
        return
    except WebSocketDisconnect:
        return

    chat_updates.active_rooms.setdefault(user_id, []).append(websocket)
    try:
        await websocket.send_json({"type": "ready"})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        chat_updates.disconnect(user_id, websocket)


@app.websocket("/ws/chat/{chatroom_id}")
async def websocket_chat(websocket: WebSocket, chatroom_id: int):
    await manager.connect(chatroom_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            if str(data["content"]).strip():
                await send_chat_message(chatroom_id, ChatMessageCreateRequest(**data))
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(chatroom_id, websocket)

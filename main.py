import os
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import secrets
import json
from pathlib import Path
import hashlib
import logging
import requests
import asyncio
import math
import time
import string
from typing import Dict, Any


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("rank_up_api")


# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="RANK UP API",
    version="2.0.0"
)


# ============================================================
# GOOGLE
# ============================================================

GOOGLE_CLIENT_ID = (
    "303662652716-767f2sap847q5h3872s57hkudsfbenjc.apps.googleusercontent.com"
)


# ============================================================
# STORAGE
# ============================================================

API_KEYS: Dict[str, dict] = {}
USERS: Dict[str, dict] = {}

# ============================================================
# PERSISTENT LOCAL USERS
# RANKUP_AUTOLOGIN_API_V1
# ============================================================

STATE_FILE = (
    Path(
        os.getenv(
            "APPDATA",
            str(Path.home())
        )
    )
    / "RANK UP"
    / "rankup_users.json"
)


def load_persisted_users():
    try:
        if not STATE_FILE.exists():
            return

        data = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

        if not isinstance(data, dict):
            return

        for google_id, user in data.items():
            if (
                isinstance(google_id, str)
                and isinstance(user, dict)
                and user.get("session_token")
            ):
                USERS[google_id] = user

        logger.info(
            "Loaded %d persisted RANK UP users.",
            len(USERS)
        )

    except Exception as exc:
        logger.warning(
            "Could not load persisted users: %s",
            exc
        )


def save_persisted_users():
    try:
        STATE_FILE.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        temp = STATE_FILE.with_suffix(
            ".tmp"
        )

        temp.write_text(
            json.dumps(
                USERS,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

        temp.replace(
            STATE_FILE
        )

    except Exception as exc:
        logger.warning(
            "Could not save persisted users: %s",
            exc
        )


load_persisted_users()



# Temporary one-time browser launch tickets.
LAUNCH_TICKETS: Dict[str, dict] = {}

# NEON CORE rooms.
ROOMS: Dict[str, dict] = {}


# ============================================================
# HELPERS
# ============================================================

def hash_api_key(api_key: str):
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def public_user(user: dict):
    return {
        "user_id": user["user_id"],
        "name": user["name"],
        "email": user["email"],
        "picture": user["picture"],
        "rank": user["rank"],
        "xp": user["xp"]
    }


def add_user_xp(user: dict, amount: int):
    amount = max(0, int(amount))

    user["xp"] += amount

    # Simple RANK UP progression.
    while user["xp"] >= user["rank"] * 1000:
        user["xp"] -= user["rank"] * 1000
        user["rank"] += 1

    return user


def generate_room_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))

        if code not in ROOMS:
            return code


def distance(x1, y1, x2, y2):
    return math.sqrt(
        (x2 - x1) ** 2 +
        (y2 - y1) ** 2
    )


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


# ============================================================
# BASIC API
# ============================================================

@app.get("/")
def root():
    return {
        "name": "RANK UP API",
        "version": "2.0.0",
        "status": "online",
        "neon_core": True
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "RANK UP API",
        "google_login": True,
        "neon_core": True,
        "neon_core_multiplayer": True
    }


# ============================================================
# APP API
# ============================================================

@app.post("/v1/apps")
def create_app(data: dict):
    name = data.get("name")
    platform = data.get("platform")

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Missing app name"
        )

    if not platform:
        raise HTTPException(
            status_code=400,
            detail="Missing platform"
        )

    app_id = "app_" + secrets.token_urlsafe(10)
    api_key = "rankup_" + secrets.token_urlsafe(32)

    API_KEYS[hash_api_key(api_key)] = {
        "app_id": app_id,
        "name": name,
        "platform": platform
    }

    return {
        "success": True,
        "app_id": app_id,
        "api_key": api_key,
        "name": name,
        "platform": platform
    }



@app.post("/v1/auth/logout")
def auth_logout(
    authorization: str = Header(default=None)
):
    if not authorization:
        return {"success": True}

    if not authorization.startswith("Bearer "):
        return {"success": True}

    token = authorization[7:].strip()

    if not token:
        return {"success": True}

    for user in USERS.values():
        if user.get("session_token") == token:
            user["session_token"] = None
            save_persisted_users()
            break

    return {"success": True}


@app.get("/v1/apps")
def list_apps():
    apps = []

    for value in API_KEYS.values():
        apps.append({
            "app_id": value["app_id"],
            "name": value["name"],
            "platform": value["platform"]
        })

    return {
        "count": len(apps),
        "apps": apps
    }


@app.get("/v1/me")
def me(x_rank_up_key: str = Header(default=None)):
    if not x_rank_up_key:
        raise HTTPException(
            status_code=401,
            detail="Missing RANK UP API key"
        )

    key_hash = hash_api_key(x_rank_up_key)
    application = API_KEYS.get(key_hash)

    if not application:
        raise HTTPException(
            status_code=401,
            detail="Invalid RANK UP API key"
        )

    return {
        "authenticated": True,
        "application": application
    }


# ============================================================
# GOOGLE AUTH
# ============================================================

@app.post("/v1/auth/google")
def google_login(data: dict):

    google_id_token = data.get("id_token")

    if not google_id_token:
        raise HTTPException(
            status_code=400,
            detail="Missing Google ID token"
        )

    try:
        response = requests.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={
                "id_token": google_id_token
            },
            timeout=15
        )

        logger.info(
            "Google tokeninfo response status: %s",
            response.status_code
        )

        if response.status_code != 200:
            logger.error(
                "Google tokeninfo failed: %s",
                response.text[:500]
            )

            raise HTTPException(
                status_code=401,
                detail="Invalid Google ID token"
            )

        google_user = response.json()

    except requests.RequestException as e:

        logger.error(
            "Could not connect to Google tokeninfo: %s",
            str(e)
        )

        raise HTTPException(
            status_code=503,
            detail="Could not connect to Google"
        )

    except ValueError:

        logger.error(
            "Google returned invalid JSON"
        )

        raise HTTPException(
            status_code=401,
            detail="Invalid Google response"
        )

    issuer = google_user.get("iss")

    if issuer not in (
        "accounts.google.com",
        "https://accounts.google.com"
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid Google token issuer"
        )

    audience = google_user.get("aud")

    if audience != GOOGLE_CLIENT_ID:
        logger.error(
            "Google audience mismatch. Received aud=%s",
            audience
        )

        raise HTTPException(
            status_code=401,
            detail="Google client ID mismatch"
        )

    google_id = google_user.get("sub")
    email = google_user.get("email")
    name = google_user.get("name")
    picture = google_user.get("picture")

    if not google_id or not email:
        raise HTTPException(
            status_code=400,
            detail="Google account information is incomplete"
        )

    email_verified = google_user.get("email_verified")

    if email_verified in ("false", False):
        raise HTTPException(
            status_code=401,
            detail="Google email is not verified"
        )

    if google_id not in USERS:

        user_id = (
            "user_" +
            secrets.token_urlsafe(10)
        )

        session_token = (
            "rankup_session_" +
            secrets.token_urlsafe(32)
        )

        USERS[google_id] = {
            "user_id": user_id,
            "google_id": google_id,
            "email": email,
            "name": name or "RANK UP User",
            "picture": picture,
            "rank": 1,
            "xp": 0,
            "session_token": session_token
        }

        created = True

        logger.info(
            "New RANK UP user created: %s",
            email
        )

    else:

        user = USERS[google_id]

        user["email"] = email
        user["name"] = name or user["name"]
        user["picture"] = picture

        if not user.get("session_token"):
            user["session_token"] = (
                "rankup_session_" +
                secrets.token_urlsafe(32)
            )

        created = False

        logger.info(
            "Existing RANK UP user logged in: %s",
            email
        )

    user = USERS[google_id]

    save_persisted_users()

    return {
        "success": True,
        "created": created,
        "user": public_user(user),
        "session_token": user["session_token"]
    }


@app.get("/v1/auth/me")
def auth_me(
    authorization: str = Header(default=None)
):

    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Missing authorization"
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Invalid authorization format"
        )

    token = authorization[7:].strip()

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Empty session token"
        )

    for user in USERS.values():

        if user["session_token"] == token:

            return {
                "authenticated": True,
                "user": public_user(user)
            }

    raise HTTPException(
        status_code=401,
        detail="Invalid session token"
    )


# ============================================================
# NEON CORE AUTH LAUNCH TICKET
# ============================================================

@app.post("/v1/neon/launch")
def neon_launch(
    authorization: str = Header(default=None)
):

    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Missing authorization"
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Invalid authorization format"
        )

    token = authorization[7:].strip()

    user = None

    for item in USERS.values():

        if item["session_token"] == token:
            user = item
            break

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid session token"
        )

    launch_token = (
        "neon_launch_" +
        secrets.token_urlsafe(32)
    )

    LAUNCH_TICKETS[launch_token] = {
        "user_id": user["user_id"],
        "created_at": time.time()
    }

    return {
        "success": True,
        "launch_token": launch_token
    }


def consume_launch_ticket(token: str):

    ticket = LAUNCH_TICKETS.pop(token, None)

    if not ticket:
        return None

    # Tickets expire after 5 minutes.
    if time.time() - ticket["created_at"] > 300:
        return None

    for user in USERS.values():

        if user["user_id"] == ticket["user_id"]:
            return user

    return None


# ============================================================
# NEON CORE GAME
# ============================================================

ARENA_WIDTH = 1100
ARENA_HEIGHT = 700

MAX_PLAYERS = 2

TICK_RATE = 20


def create_player(user: dict, slot: int):

    if slot == 1:
        x = 300
        y = ARENA_HEIGHT / 2
    else:
        x = 800
        y = ARENA_HEIGHT / 2

    return {
        "user_id": user["user_id"],
        "name": user["name"],
        "slot": slot,

        "x": x,
        "y": y,

        "hp": 100,
        "max_hp": 100,

        "speed": 230,
        "damage": 25,

        "fire_delay": 0.20,
        "next_shot": 0,

        "score": 0,
        "xp": 0,
        "level": 1,

        "kills": 0,

        "downed": False,
        "downed_until": 0,
        "eliminated": False,

        "revive_progress": 0,

        "input": {
            "up": False,
            "down": False,
            "left": False,
            "right": False,
            "shoot": False,
            "revive": False,
            "angle": 0
        },

        "socket": None,
        "connected": False,

        "pending_upgrade": None
    }


def create_room(user: dict):

    code = generate_room_code()

    room = {
        "code": code,

        "players": {
            1: create_player(user, 1)
        },

        "enemies": [],
        "bullets": [],

        "wave": 0,

        "status": "waiting",

        "next_wave_time": 0,

        "wave_message": "",

        "wave_message_until": 0,

        "created_at": time.time(),

        "winner": False,

        "xp_awarded": False
    }

    ROOMS[code] = room

    logger.info(
        "NEON CORE room created: %s",
        code
    )

    return room


def spawn_wave(room: dict):

    room["wave"] += 1

    wave = room["wave"]

    room["enemies"] = []

    if wave == 10:

        room["enemies"].append({
            "id": secrets.token_hex(6),
            "kind": "boss",

            "x": ARENA_WIDTH / 2,
            "y": 100,

            "hp": 5000,
            "max_hp": 5000,

            "speed": 55,
            "radius": 45,

            "damage": 20,
            "attack_timer": 0,

            "score": 2500
        })

    else:

        count = 5 + wave * 2

        for i in range(count):

            side = i % 4

            if side == 0:
                x = 40
                y = secrets.randbelow(ARENA_HEIGHT - 80) + 40

            elif side == 1:
                x = ARENA_WIDTH - 40
                y = secrets.randbelow(ARENA_HEIGHT - 80) + 40

            elif side == 2:
                x = secrets.randbelow(ARENA_WIDTH - 80) + 40
                y = 40

            else:
                x = secrets.randbelow(ARENA_WIDTH - 80) + 40
                y = ARENA_HEIGHT - 40

            elite = (
                wave >= 3 and
                i % 7 == 0
            )

            hp = (
                120 + wave * 25
                if elite
                else
                50 + wave * 15
            )

            room["enemies"].append({
                "id": secrets.token_hex(6),
                "kind": "elite" if elite else "normal",

                "x": x,
                "y": y,

                "hp": hp,
                "max_hp": hp,

                "speed": (
                    55 + wave * 3
                    if elite
                    else
                    65 + wave * 4
                ),

                "radius": 22 if elite else 16,

                "damage": (
                    18 + wave
                    if elite
                    else
                    10 + wave
                ),

                "attack_timer": 0,

                "score": (
                    250
                    if elite
                    else
                    100
                )
            })

    room["wave_message"] = (
        "BOSS WAVE" if wave == 10
        else f"WAVE {wave}"
    )

    room["wave_message_until"] = time.time() + 3


def living_players(room: dict):

    return [
        p for p in room["players"].values()
        if not p["eliminated"]
    ]


def alive_players(room: dict):

    return [
        p for p in room["players"].values()
        if not p["eliminated"] and not p["downed"]
    ]


def choose_target(room: dict, enemy: dict):

    targets = alive_players(room)

    if not targets:
        return None

    targets.sort(
        key=lambda p: distance(
            enemy["x"],
            enemy["y"],
            p["x"],
            p["y"]
        )
    )

    return targets[0]


def give_xp(player: dict, amount: int):

    player["xp"] += amount

    required = player["level"] * 250

    if player["xp"] >= required:

        player["xp"] -= required
        player["level"] += 1

        choices = [
            {
                "id": "damage",
                "title": "+ DAMAGE",
                "description": "Weapon damage +10"
            },
            {
                "id": "speed",
                "title": "+ SPEED",
                "description": "Movement speed +35"
            },
            {
                "id": "fire",
                "title": "+ FIRE RATE",
                "description": "Fire rate +15%"
            }
        ]

        if player["level"] % 2 == 0:

            choices[2] = {
                "id": "health",
                "title": "+ MAX HP",
                "description": "Maximum HP +25"
            }

        player["pending_upgrade"] = choices


def apply_upgrade(player: dict, upgrade: str):

    if player["pending_upgrade"] is None:
        return

    valid = [
        item["id"]
        for item in player["pending_upgrade"]
    ]

    if upgrade not in valid:
        return

    if upgrade == "damage":
        player["damage"] += 10

    elif upgrade == "speed":
        player["speed"] += 35

    elif upgrade == "fire":
        player["fire_delay"] *= 0.85

    elif upgrade == "health":
        player["max_hp"] += 25
        player["hp"] = min(
            player["max_hp"],
            player["hp"] + 25
        )

    player["pending_upgrade"] = None


def player_shoot(room: dict, player: dict):

    now = time.time()

    if now < player["next_shot"]:
        return

    angle = player["input"]["angle"]

    vx = math.cos(angle) * 650
    vy = math.sin(angle) * 650

    room["bullets"].append({
        "id": secrets.token_hex(5),

        "owner": player["slot"],

        "x": player["x"],
        "y": player["y"],

        "vx": vx,
        "vy": vy,

        "damage": player["damage"],

        "life": 1.5
    })

    player["next_shot"] = now + player["fire_delay"]


def update_player(room: dict, player: dict, dt: float):

    if player["eliminated"]:
        return

    now = time.time()

    if player["downed"]:

        if now >= player["downed_until"]:

            player["downed"] = False
            player["eliminated"] = True
            player["revive_progress"] = 0

        return

    move_x = 0
    move_y = 0

    if player["input"]["left"]:
        move_x -= 1

    if player["input"]["right"]:
        move_x += 1

    if player["input"]["up"]:
        move_y -= 1

    if player["input"]["down"]:
        move_y += 1

    if move_x != 0 or move_y != 0:

        length = math.sqrt(
            move_x * move_x +
            move_y * move_y
        )

        move_x /= length
        move_y /= length

        player["x"] += (
            move_x *
            player["speed"] *
            dt
        )

        player["y"] += (
            move_y *
            player["speed"] *
            dt
        )

    player["x"] = clamp(
        player["x"],
        25,
        ARENA_WIDTH - 25
    )

    player["y"] = clamp(
        player["y"],
        25,
        ARENA_HEIGHT - 25
    )

    if player["input"]["shoot"]:
        player_shoot(room, player)


def damage_player(player: dict, damage: int):

    if player["downed"] or player["eliminated"]:
        return

    player["hp"] -= damage

    if player["hp"] <= 0:

        player["hp"] = 0

        player["downed"] = True

        player["downed_until"] = (
            time.time() + 10
        )

        player["revive_progress"] = 0


def update_revive(room: dict, dt: float):

    players = list(room["players"].values())

    for target in players:

        if not target["downed"]:
            target["revive_progress"] = 0
            continue

        reviver = None

        for other in players:

            if other["slot"] == target["slot"]:
                continue

            if other["eliminated"] or other["downed"]:
                continue

            if not other["input"]["revive"]:
                continue

            if distance(
                target["x"],
                target["y"],
                other["x"],
                other["y"]
            ) <= 75:

                reviver = other
                break

        if reviver:

            target["revive_progress"] += dt

            if target["revive_progress"] >= 2.5:

                target["downed"] = False
                target["hp"] = int(
                    target["max_hp"] * 0.4
                )

                target["downed_until"] = 0
                target["revive_progress"] = 0

                reviver["score"] += 300

        else:

            target["revive_progress"] = max(
                0,
                target["revive_progress"] - dt * 2
            )


def update_bullets(room: dict, dt: float):

    new_bullets = []

    for bullet in room["bullets"]:

        bullet["x"] += bullet["vx"] * dt
        bullet["y"] += bullet["vy"] * dt
        bullet["life"] -= dt

        if bullet["life"] <= 0:
            continue

        if not (
            0 <= bullet["x"] <= ARENA_WIDTH and
            0 <= bullet["y"] <= ARENA_HEIGHT
        ):
            continue

        hit = False

        for enemy in room["enemies"]:

            if enemy["hp"] <= 0:
                continue

            if distance(
                bullet["x"],
                bullet["y"],
                enemy["x"],
                enemy["y"]
            ) <= enemy["radius"] + 5:

                enemy["hp"] -= bullet["damage"]

                hit = True

                if enemy["hp"] <= 0:

                    player = room["players"].get(
                        bullet["owner"]
                    )

                    if player:

                        player["score"] += enemy["score"]
                        player["kills"] += 1

                        give_xp(
                            player,
                            100 if enemy["kind"] == "boss"
                            else 30
                        )

                break

        if not hit:
            new_bullets.append(bullet)

    room["bullets"] = new_bullets

    room["enemies"] = [
        enemy
        for enemy in room["enemies"]
        if enemy["hp"] > 0
    ]


def update_enemies(room: dict, dt: float):

    for enemy in room["enemies"]:

        target = choose_target(
            room,
            enemy
        )

        if target is None:
            continue

        dx = target["x"] - enemy["x"]
        dy = target["y"] - enemy["y"]

        length = math.sqrt(
            dx * dx +
            dy * dy
        )

        if length > 1:

            dx /= length
            dy /= length

            enemy["x"] += (
                dx *
                enemy["speed"] *
                dt
            )

            enemy["y"] += (
                dy *
                enemy["speed"] *
                dt
            )

        enemy["attack_timer"] -= dt

        if distance(
            enemy["x"],
            enemy["y"],
            target["x"],
            target["y"]
        ) < enemy["radius"] + 18:

            if enemy["attack_timer"] <= 0:

                damage_player(
                    target,
                    enemy["damage"]
                )

                enemy["attack_timer"] = (
                    0.7
                )


def start_game(room: dict):

    if room["status"] != "waiting":
        return

    if len(room["players"]) < 2:
        return

    room["status"] = "playing"

    spawn_wave(room)


def finish_room(room: dict, won: bool):

    if room["status"] in (
        "won",
        "lost"
    ):
        return

    room["status"] = (
        "won" if won else "lost"
    )

    if room["xp_awarded"]:
        return

    room["xp_awarded"] = True

    for player in room["players"].values():

        for user in USERS.values():

            if user["user_id"] == player["user_id"]:

                match_xp = (
                    500
                    if won
                    else 100
                )

                match_xp += (
                    room["wave"] * 20
                )

                match_xp += (
                    player["score"] // 100
                )

                add_user_xp(
                    user,
                    match_xp
                )

                break


def update_room(room: dict, dt: float):

    if room["status"] == "waiting":
        return

    if room["status"] in (
        "won",
        "lost"
    ):
        return

    for player in room["players"].values():
        update_player(
            room,
            player,
            dt
        )

    update_revive(
        room,
        dt
    )

    update_bullets(
        room,
        dt
    )

    update_enemies(
        room,
        dt
    )

    living = living_players(room)

    if not living:

        finish_room(
            room,
            False
        )

        return

    # Wave cleared.
    if not room["enemies"]:

        if room["wave"] >= 10:

            finish_room(
                room,
                True
            )

            return

        if room["next_wave_time"] == 0:

            room["next_wave_time"] = (
                time.time() + 2
            )

        elif time.time() >= room["next_wave_time"]:

            room["next_wave_time"] = 0

            for player in room["players"].values():

                if not player["eliminated"]:

                    player["score"] += 500

            spawn_wave(room)


async def game_loop():

    previous = time.time()

    while True:

        now = time.time()

        dt = min(
            now - previous,
            0.1
        )

        previous = now

        for room in list(ROOMS.values()):

            try:
                update_room(
                    room,
                    dt
                )

            except Exception as e:

                logger.exception(
                    "NEON CORE room update error: %s",
                    e
                )

        await asyncio.sleep(
            1 / TICK_RATE
        )


@app.on_event("startup")
async def startup_event():

    asyncio.create_task(
        game_loop()
    )

    logger.info(
        "NEON CORE game loop started"
    )


# ============================================================
# ROOM API
# ============================================================

@app.post("/v1/neon/rooms")
def create_neon_room(
    authorization: str = Header(default=None)
):

    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Missing authorization"
        )

    token = authorization.replace(
        "Bearer ",
        "",
        1
    ).strip()

    user = None

    for item in USERS.values():

        if item["session_token"] == token:
            user = item
            break

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid session token"
        )

    room = create_room(user)

    return {
        "success": True,
        "room_code": room["code"],
        "players": 1,
        "max_players": 2
    }


@app.get("/v1/neon/rooms/{room_code}")
def neon_room_info(room_code: str):

    code = room_code.upper().strip()

    room = ROOMS.get(code)

    if not room:
        raise HTTPException(
            status_code=404,
            detail="Room not found"
        )

    return {
        "success": True,
        "room_code": code,
        "players": len(room["players"]),
        "max_players": 2,
        "status": room["status"]
    }


# ============================================================
# WEBSOCKET
# ============================================================

def serialize_player(player: dict):

    return {
        "slot": player["slot"],
        "name": player["name"],

        "x": round(player["x"], 1),
        "y": round(player["y"], 1),

        "hp": player["hp"],
        "max_hp": player["max_hp"],

        "score": player["score"],
        "xp": player["xp"],
        "level": player["level"],
        "kills": player["kills"],

        "downed": player["downed"],
        "eliminated": player["eliminated"],

        "revive_progress": round(
            player["revive_progress"],
            2
        ),

        "pending_upgrade": player["pending_upgrade"]
    }


def serialize_room(room: dict):

    return {
        "type": "state",

        "arena": {
            "width": ARENA_WIDTH,
            "height": ARENA_HEIGHT
        },

        "status": room["status"],

        "wave": room["wave"],

        "wave_message": room["wave_message"]
        if time.time() < room["wave_message_until"]
        else "",

        "players": [
            serialize_player(player)
            for player in room["players"].values()
        ],

        "enemies": [
            {
                "id": enemy["id"],
                "kind": enemy["kind"],
                "x": round(enemy["x"], 1),
                "y": round(enemy["y"], 1),
                "hp": enemy["hp"],
                "max_hp": enemy["max_hp"],
                "radius": enemy["radius"]
            }
            for enemy in room["enemies"]
        ],

        "bullets": [
            {
                "x": round(bullet["x"], 1),
                "y": round(bullet["y"], 1)
            }
            for bullet in room["bullets"]
        ]
    }


@app.websocket("/ws/neon/{launch_token}")
async def neon_websocket(
    websocket: WebSocket,
    launch_token: str
):

    await websocket.accept()

    user = consume_launch_ticket(
        launch_token
    )

    if not user:

        await websocket.send_json({
            "type": "error",
            "message": "Invalid or expired launch ticket."
        })

        await websocket.close()

        return

    player = None
    room = None

    try:

        await websocket.send_json({
            "type": "authenticated",
            "name": user["name"]
        })

        while True:

            message = await websocket.receive_json()

            message_type = message.get(
                "type"
            )

            # ------------------------------------------------
            # CREATE ROOM
            # ------------------------------------------------

            if message_type == "create_room":

                if room is not None:
                    continue

                room = create_room(user)

                player = room["players"][1]

                player["socket"] = websocket
                player["connected"] = True

                await websocket.send_json({
                    "type": "room_created",
                    "room_code": room["code"]
                })

                continue

            # ------------------------------------------------
            # JOIN ROOM
            # ------------------------------------------------

            if message_type == "join_room":

                code = str(
                    message.get("room_code", "")
                ).upper().strip()

                target_room = ROOMS.get(code)

                if not target_room:

                    await websocket.send_json({
                        "type": "error",
                        "message": "Room not found."
                    })

                    continue

                # Existing player reconnect.
                existing = None

                for p in target_room["players"].values():

                    if p["user_id"] == user["user_id"]:
                        existing = p
                        break

                if existing:

                    player = existing
                    room = target_room

                else:

                    if len(target_room["players"]) >= MAX_PLAYERS:

                        await websocket.send_json({
                            "type": "error",
                            "message": "Room is full."
                        })

                        continue

                    player = create_player(
                        user,
                        2
                    )

                    target_room["players"][2] = player

                    room = target_room

                player["socket"] = websocket
                player["connected"] = True

                await websocket.send_json({
                    "type": "joined",
                    "room_code": room["code"],
                    "slot": player["slot"]
                })

                if len(room["players"]) >= 2:

                    start_game(room)

                continue

            # ------------------------------------------------
            # INPUT
            # ------------------------------------------------

            if message_type == "input":

                if player is None:
                    continue

                data = message.get(
                    "data",
                    {}
                )

                player["input"] = {
                    "up": bool(
                        data.get("up", False)
                    ),

                    "down": bool(
                        data.get("down", False)
                    ),

                    "left": bool(
                        data.get("left", False)
                    ),

                    "right": bool(
                        data.get("right", False)
                    ),

                    "shoot": bool(
                        data.get("shoot", False)
                    ),

                    "revive": bool(
                        data.get("revive", False)
                    ),

                    "angle": float(
                        data.get("angle", 0)
                    )
                }

                continue

            # ------------------------------------------------
            # UPGRADE
            # ------------------------------------------------

            if message_type == "upgrade":

                if player is None:
                    continue

                choice = str(
                    message.get(
                        "choice",
                        ""
                    )
                )

                apply_upgrade(
                    player,
                    choice
                )

                continue

            # ------------------------------------------------
            # PING
            # ------------------------------------------------

            if message_type == "ping":

                await websocket.send_json({
                    "type": "pong"
                })

    except WebSocketDisconnect:

        logger.info(
            "NEON CORE websocket disconnected: %s",
            user["email"]
        )

        if player:

            player["connected"] = False
            player["socket"] = None

    except Exception as e:

        logger.exception(
            "NEON websocket error: %s",
            e
        )

        if player:

            player["connected"] = False
            player["socket"] = None


# ============================================================
# BROADCAST LOOP
# ============================================================

async def broadcast_loop():

    while True:

        for room in list(ROOMS.values()):

            if not room["players"]:
                continue

            state = serialize_room(room)

            for player in list(
                room["players"].values()
            ):

                socket = player.get(
                    "socket"
                )

                if not socket:
                    continue

                try:

                    await socket.send_json(
                        state
                    )

                except Exception:

                    player["connected"] = False
                    player["socket"] = None

        await asyncio.sleep(
            1 / TICK_RATE
        )


@app.on_event("startup")
async def broadcast_startup():

    asyncio.create_task(
        broadcast_loop()
    )


# ============================================================
# NEON CORE CLIENT
# ============================================================

NEON_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>NEON CORE</title>
<!-- RANK UP ONLINE ORIGINAL NEON CORE V1 -->

<style>
*{
    box-sizing:border-box;
    margin:0;
    padding:0;
}

html,body{
    width:100%;
    height:100%;
    overflow:hidden;
    background:#03050b;
    font-family:Arial,Helvetica,sans-serif;
}

body{
    display:flex;
    align-items:center;
    justify-content:center;
}

canvas{
    display:block;
    background:
        radial-gradient(circle at center,#07101d 0%,#03050b 70%);
    cursor:crosshair;
}

#ui{
    position:fixed;
    top:18px;
    left:18px;
    right:18px;
    display:flex;
    justify-content:space-between;
    pointer-events:none;
    color:white;
    z-index:10;
}

.panel{
    background:rgba(3,7,18,.78);
    border:1px solid rgba(0,220,255,.35);
    box-shadow:
        0 0 20px rgba(0,180,255,.12),
        inset 0 0 20px rgba(0,180,255,.04);
    backdrop-filter:blur(8px);
    border-radius:12px;
    padding:10px 14px;
}

.stat{
    min-width:190px;
}

.label{
    font-size:11px;
    color:#79cfff;
    letter-spacing:2px;
}

.value{
    font-size:20px;
    font-weight:bold;
    margin-top:3px;
}

.bar{
    width:190px;
    height:8px;
    background:#111827;
    border-radius:10px;
    margin-top:7px;
    overflow:hidden;
}

.bar > div{
    height:100%;
    width:100%;
    transition:width .15s;
}

#hpBar{
    background:linear-gradient(90deg,#ff164f,#ff5d77);
    box-shadow:0 0 12px #ff164f;
}

#xpBar{
    background:linear-gradient(90deg,#00d9ff,#9c5cff);
    box-shadow:0 0 12px #00d9ff;
}

#centerMessage{
    position:fixed;
    inset:0;
    display:flex;
    align-items:center;
    justify-content:center;
    pointer-events:none;
    z-index:20;
}

.overlay{
    width:min(700px,90vw);
    text-align:center;
    padding:40px;
    background:rgba(2,5,13,.92);
    border:1px solid rgba(0,220,255,.45);
    box-shadow:
        0 0 70px rgba(0,180,255,.16),
        inset 0 0 50px rgba(0,180,255,.04);
    border-radius:22px;
    pointer-events:auto;
}

.overlay h1{
    font-size:clamp(42px,8vw,90px);
    color:#fff;
    letter-spacing:8px;
    text-shadow:
        0 0 10px #00d9ff,
        0 0 30px #00d9ff;
}

.overlay h2{
    color:#00d9ff;
    margin:10px 0 20px;
    letter-spacing:3px;
}

.overlay p{
    color:#b8c9d9;
    line-height:1.8;
}

button{
    margin-top:25px;
    border:none;
    padding:14px 30px;
    border-radius:10px;
    color:white;
    background:linear-gradient(90deg,#0077ff,#a000ff);
    font-weight:bold;
    font-size:16px;
    cursor:pointer;
    box-shadow:0 0 25px rgba(80,0,255,.35);
    transition:.2s;
}

button:hover{
    transform:translateY(-2px) scale(1.03);
    box-shadow:0 0 35px rgba(0,200,255,.5);
}

.hidden{
    display:none !important;
}

#upgradeScreen{
    position:fixed;
    inset:0;
    background:rgba(0,0,0,.65);
    display:flex;
    align-items:center;
    justify-content:center;
    z-index:30;
}

.upgradeBox{
    width:min(850px,92vw);
    text-align:center;
}

.upgradeBox h2{
    color:white;
    font-size:35px;
    margin-bottom:25px;
    text-shadow:0 0 20px #00d9ff;
}

.cards{
    display:flex;
    gap:18px;
    justify-content:center;
}

.card{
    flex:1;
    min-height:180px;
    padding:25px 18px;
    background:rgba(6,12,28,.96);
    border:1px solid #17314b;
    border-radius:16px;
    color:white;
    cursor:pointer;
    transition:.2s;
}

.card:hover{
    transform:translateY(-8px);
    border-color:#00d9ff;
    box-shadow:0 0 35px rgba(0,217,255,.25);
}

.card .icon{
    font-size:42px;
    margin-bottom:12px;
}

.card h3{
    color:#00d9ff;
    margin-bottom:10px;
}

.card p{
    color:#aebccc;
    font-size:14px;
    line-height:1.5;
}

#toast{
    position:fixed;
    left:50%;
    bottom:30px;
    transform:translateX(-50%);
    padding:10px 20px;
    color:white;
    background:rgba(5,10,20,.9);
    border:1px solid rgba(0,217,255,.4);
    border-radius:10px;
    opacity:0;
    transition:.3s;
    z-index:50;
}

#controls{
    position:fixed;
    bottom:18px;
    left:18px;
    color:#64788d;
    font-size:12px;
    pointer-events:none;
}

@media(max-width:700px){
    .cards{
        flex-direction:column;
    }

    .card{
        min-height:130px;
    }

    #controls{
        display:none;
    }

    .stat{
        min-width:140px;
    }

    .bar{
        width:140px;
    }
}

#onlineRoomControls{
    opacity:.98;
}

#onlineRoomControls button:disabled{
    opacity:.45;
    cursor:not-allowed;
    transform:none;
}

#onlineConnection.error{
    color:#ff6685;
}

#roomCodeDisplay strong{
    color:#00d9ff;
}

#remotePlayerLabel{
    position:fixed;
    left:0;
    top:0;
    z-index:12;
    pointer-events:none;
    color:#ff7cf3;
    font-size:11px;
    font-weight:bold;
    letter-spacing:1px;
    text-shadow:0 0 10px rgba(255,80,235,.8);
}

</style>
</head>

<body>

<canvas id="game"></canvas>

<div id="ui">

    <div class="panel stat">
        <div class="label">CORE INTEGRITY</div>

        <div class="value">
            <span id="hpText">100</span> / 100
        </div>

        <div class="bar">
            <div id="hpBar"></div>
        </div>

        <div class="label" style="margin-top:9px;">
            ENERGY XP
        </div>

        <div class="bar">
            <div id="xpBar"></div>
        </div>
    </div>

    <div class="panel" style="text-align:right;">

        <div class="label">WAVE</div>
        <div class="value" id="waveText">1</div>

        <div class="label" style="margin-top:7px;">
            SCORE
        </div>

        <div class="value" id="scoreText">0</div>

        <div class="label" style="margin-top:7px;">
            BEST
        </div>

        <div class="value" id="bestText">0</div>

    </div>

</div>

<div id="controls">
    WASD / ARROWS = MOVE &nbsp;&nbsp; • &nbsp;&nbsp;
    MOUSE = AIM &nbsp;&nbsp; • &nbsp;&nbsp;
    LEFT CLICK = FIRE &nbsp;&nbsp; • &nbsp;&nbsp; E = REVIVE
</div>

<div id="centerMessage">

    <div class="overlay" id="startScreen">

        <h1>NEON</h1>
        <h2>CORE</h2>

        <p>
            SURVIVE THE ARENA.<br>
            DESTROY THE HOSTILES.<br>
            REACH WAVE 20.<br>
            BECOME THE CORE.
        </p>

        <button id="startButton">
            START MISSION
        </button>

        <div id="onlineRoomControls" style="margin-top:22px;">
            <div id="onlineConnection" style="
                color:#79cfff;
                font-size:12px;
                letter-spacing:1px;
                margin-bottom:12px;
            ">CONNECTING TO RANK UP...</div>

            <div style="
                display:flex;
                gap:10px;
                flex-wrap:wrap;
                justify-content:center;
            ">
                <button id="createRoomButton" style="margin-top:0;">
                    CREATE ROOM
                </button>
                <button id="startButtonOnline" style="margin-top:0;display:none;">
                    ENTER ARENA
                </button>
            </div>

            <div style="
                display:flex;
                gap:10px;
                margin-top:10px;
                justify-content:center;
                flex-wrap:wrap;
            ">
                <input
                    id="roomCodeInput"
                    maxlength="6"
                    placeholder="ROOM CODE"
                    autocomplete="off"
                    style="
                        width:210px;
                        text-align:center;
                        text-transform:uppercase;
                        letter-spacing:4px;
                        background:#071020;
                        color:white;
                        border:1px solid #315477;
                        border-radius:10px;
                        padding:12px;
                        outline:none;
                    "
                >
                <button id="joinRoomButton" style="margin-top:0;">
                    JOIN ROOM
                </button>
            </div>

            <div id="roomCodeDisplay" style="
                min-height:26px;
                margin-top:12px;
                color:#ffffff;
                font-weight:bold;
                letter-spacing:3px;
            "></div>
        </div>

    </div>

    <div class="overlay hidden" id="gameOverScreen">

        <h1 style="font-size:50px;">
            SYSTEM FAILURE
        </h1>

        <h2>CORE DESTROYED</h2>

        <p>
            SCORE:
            <strong id="finalScore">0</strong>
            <br>
            WAVE:
            <strong id="finalWave">1</strong>
            <br>
            BEST:
            <strong id="finalBest">0</strong>
        </p>

        <button id="restartButton">
            PLAY AGAIN
        </button>

    </div>

    <div class="overlay hidden" id="victoryScreen">

        <h1 style="font-size:50px;">
            VICTORY
        </h1>

        <h2>CORE ASCENDED</h2>

        <p>
            YOU SURVIVED ALL 20 WAVES.<br>
            FINAL SCORE:
            <strong id="victoryScore">0</strong>
            <br>
            BEST:
            <strong id="victoryBest">0</strong>
        </p>

        <button id="victoryButton">
            PLAY AGAIN
        </button>

    </div>

</div>

<div id="upgradeScreen" class="hidden">

    <div class="upgradeBox">

        <h2>⚡ LEVEL UP — CHOOSE UPGRADE</h2>

        <div class="cards">

            <div class="card" data-upgrade="fire">
                <div class="icon">⚡</div>
                <h3 id="upgrade1Title">
                    RAPID FIRE
                </h3>
                <p id="upgrade1Text">
                    Increase weapon fire rate.
                </p>
            </div>

            <div class="card" data-upgrade="power">
                <div class="icon">💥</div>
                <h3 id="upgrade2Title">
                    POWER SHOT
                </h3>
                <p id="upgrade2Text">
                    Increase bullet damage.
                </p>
            </div>

            <div class="card" data-upgrade="repair">
                <div class="icon">❤️</div>
                <h3 id="upgrade3Title">
                    NANO REPAIR
                </h3>
                <p id="upgrade3Text">
                    Restore HP.
                </p>
            </div>

        </div>

    </div>
</div>

<div id="toast"></div>
<div id="remotePlayerLabel"></div>

<script>

const canvas =
    document.getElementById("game");

const ctx =
    canvas.getContext("2d");

let W = innerWidth;
let H = innerHeight;

canvas.width = W;
canvas.height = H;

addEventListener("resize",()=>{

    W = innerWidth;
    H = innerHeight;

    canvas.width = W;
    canvas.height = H;
});

const keys = {};

addEventListener("keydown",e=>{

    keys[e.key.toLowerCase()] = true;

    if(
        e.key.toLowerCase()==="r" &&
        gameOver
    ){
        restart();
    }
});

addEventListener("keyup",e=>{

    keys[e.key.toLowerCase()] = false;
});

const mouse = {

    x:W/2,
    y:H/2,
    down:false
};

canvas.addEventListener(
    "mousemove",
    e=>{
        mouse.x=e.clientX;
        mouse.y=e.clientY;
    }
);

canvas.addEventListener(
    "mousedown",
    ()=>{
        mouse.down=true;
    }
);

addEventListener(
    "mouseup",
    ()=>{
        mouse.down=false;
    }
);

const startScreen =
    document.getElementById("startScreen");

const gameOverScreen =
    document.getElementById("gameOverScreen");

const victoryScreen =
    document.getElementById("victoryScreen");

const upgradeScreen =
    document.getElementById("upgradeScreen");

const startButton =
    document.getElementById("startButton");

const restartButton =
    document.getElementById("restartButton");

const victoryButton =
    document.getElementById("victoryButton");

const hpText =
    document.getElementById("hpText");

const hpBar =
    document.getElementById("hpBar");

const xpBar =
    document.getElementById("xpBar");

const scoreText =
    document.getElementById("scoreText");

const waveText =
    document.getElementById("waveText");

const bestText =
    document.getElementById("bestText");

let bestScore =
    Number(
        localStorage.getItem(
            "neonCoreBest"
        ) || 0
    );

bestText.textContent=bestScore;

let running=false;
let gameOver=false;
let victory=false;
let paused=false;

let score=0;
let wave=1;

const FINAL_WAVE=20;

let xp=0;
let xpNeeded=100;

let enemies=[];
let bullets=[];
let particles=[];
let cores=[];

let spawnTimer=0;
let waveTimer=0;


/* =========================
   PLAYER
========================= */

const player={

    x:W/2,
    y:H/2,

    radius:17,

    speed:4.2,

    hp:100,
    maxHp:100,

    damage:25,

    fireRate:180,
    lastShot:0,

    magnetRange:90,

    criticalChance:0,

    shield:0,

    multishot:1
};


function resetPlayer(){

    player.x=W/2;
    player.y=H/2;

    player.hp=100;
    player.maxHp=100;

    player.speed=4.2;

    player.damage=25;

    player.fireRate=180;
    player.lastShot=0;

    player.magnetRange=90;

    player.criticalChance=0;

    player.shield=0;

    player.multishot=1;
}


/* =========================
   RANDOM UPGRADES
========================= */

const upgradePool=[

    {
        id:"fire",
        icon:"⚡",
        title:"RAPID FIRE",
        text:"Increase weapon fire rate.",
    },

    {
        id:"power",
        icon:"💥",
        title:"POWER SHOT",
        text:"Increase bullet damage.",
    },

    {
        id:"repair",
        icon:"❤️",
        title:"NANO REPAIR",
        text:"Restore 30 HP and increase maximum health.",
    },

    {
        id:"speed",
        icon:"🏃",
        title:"OVERDRIVE",
        text:"Increase movement speed.",
    },

    {
        id:"magnet",
        icon:"🧲",
        title:"CORE MAGNET",
        text:"Collect dropped cores from farther away.",
    },

    {
        id:"critical",
        icon:"🎯",
        title:"CRITICAL CORE",
        text:"Increase chance for double damage.",
    },

    {
        id:"shield",
        icon:"🛡️",
        title:"ENERGY SHIELD",
        text:"Gain a shield that blocks enemy damage.",
    },

    {
        id:"multi",
        icon:"🔱",
        title:"MULTI SHOT",
        text:"Fire an additional projectile.",
    }

];


function randomUpgrades(){

    const shuffled=
        [...upgradePool]
        .sort(
            ()=>Math.random()-.5
        );

    return shuffled.slice(0,3);
}


function showRandomUpgrades(){

    const choices=
        randomUpgrades();

    const cards=
        document.querySelectorAll(
            ".card"
        );

    cards.forEach(
        (card,index)=>{

            const upgrade=
                choices[index];

            card.dataset.upgrade=
                upgrade.id;

            card.querySelector(
                ".icon"
            ).textContent=
                upgrade.icon;

            card.querySelector(
                "h3"
            ).textContent=
                upgrade.title;

            card.querySelector(
                "p"
            ).textContent=
                upgrade.text;
        }
    );

    upgradeScreen
        .classList
        .remove("hidden");
}


/* =========================
   START
========================= */

function startGame(){

    startScreen.classList.add(
        "hidden"
    );

    gameOverScreen.classList.add(
        "hidden"
    );

    victoryScreen.classList.add(
        "hidden"
    );

    score=0;

    wave=1;

    xp=0;

    xpNeeded=100;

    enemies=[];
    bullets=[];
    particles=[];
    cores=[];

    spawnTimer=0;
    waveTimer=0;

    resetPlayer();

    gameOver=false;
    victory=false;

    running=true;
    paused=false;

    updateUI();
}


startButton.onclick=startGame;
restartButton.onclick=startGame;
victoryButton.onclick=startGame;


/* =========================
   ENEMIES
========================= */

function spawnEnemy(){

    const side=
        Math.floor(
            Math.random()*4
        );

    let x,y;

    if(side===0){

        x=Math.random()*W;
        y=-40;
    }

    if(side===1){

        x=W+40;
        y=Math.random()*H;
    }

    if(side===2){

        x=Math.random()*W;
        y=H+40;
    }

    if(side===3){

        x=-40;
        y=Math.random()*H;
    }

    const elite=
        Math.random()<
        Math.min(
            .08+wave*.008,
            .3
        );

    enemies.push({

        x,
        y,

        radius:
            elite
            ?23
            :15,

        hp:
            elite
            ?100+wave*20
            :40+wave*8,

        maxHp:
            elite
            ?100+wave*20
            :40+wave*8,

        speed:
            elite
            ?0.7+wave*.025
            :1.05+wave*.045,

        damage:
            elite
            ?22
            :10,

        elite,

        color:
            elite
            ?"#ff2d78"
            :"#ff3b30"
    });
}


/* =========================
   SHOOT
========================= */

function shoot(){

    const now=
        performance.now();

    if(
        now-player.lastShot<
        player.fireRate
    ){
        return;
    }

    player.lastShot=now;

    const angle=
        Math.atan2(
            mouse.y-player.y,
            mouse.x-player.x
        );

    const speed=10;

    const count=
        player.multishot;

    const spread=.16;

    for(
        let i=0;
        i<count;
        i++
    ){

        let shotAngle=angle;

        if(count>1){

            shotAngle=
                angle+
                (
                    i-
                    (count-1)/2
                )*
                spread;
        }

        let damage=
            player.damage;

        if(
            Math.random()<
            player.criticalChance
        ){

            damage*=2;

            createParticles(
                player.x,
                player.y,
                "#fff000",
                6,
                2
            );
        }

        bullets.push({

            x:player.x,
            y:player.y,

            vx:
                Math.cos(shotAngle)*
                speed,

            vy:
                Math.sin(shotAngle)*
                speed,

            radius:5,

            damage,

            life:80
        });
    }

    createParticles(
        player.x,
        player.y,
        "#00eaff",
        4,
        2
    );
}


/* =========================
   PLAYER UPDATE
========================= */

function updatePlayer(){

    let dx=0;
    let dy=0;

    if(
        keys["w"] ||
        keys["arrowup"]
    ){
        dy--;
    }

    if(
        keys["s"] ||
        keys["arrowdown"]
    ){
        dy++;
    }

    if(
        keys["a"] ||
        keys["arrowleft"]
    ){
        dx--;
    }

    if(
        keys["d"] ||
        keys["arrowright"]
    ){
        dx++;
    }

    if(dx || dy){

        const len=
            Math.hypot(dx,dy);

        dx/=len;
        dy/=len;

        player.x+=
            dx*player.speed;

        player.y+=
            dy*player.speed;
    }

    player.x=
        Math.max(
            player.radius,
            Math.min(
                W-player.radius,
                player.x
            )
        );

    player.y=
        Math.max(
            player.radius,
            Math.min(
                H-player.radius,
                player.y
            )
        );

    if(mouse.down){
        shoot();
    }
}


/* =========================
   BULLETS
========================= */

function updateBullets(){

    for(
        let i=bullets.length-1;
        i>=0;
        i--
    ){

        const b=bullets[i];

        b.x+=b.vx;
        b.y+=b.vy;

        b.life--;

        if(
            b.life<=0 ||
            b.x<-100 ||
            b.x>W+100 ||
            b.y<-100 ||
            b.y>H+100
        ){

            bullets.splice(i,1);
            continue;
        }

        for(
            let j=enemies.length-1;
            j>=0;
            j--
        ){

            const e=enemies[j];

            const dist=
                Math.hypot(
                    b.x-e.x,
                    b.y-e.y
                );

            if(
                dist<
                b.radius+e.radius
            ){

                e.hp-=b.damage;

                createParticles(
                    b.x,
                    b.y,
                    e.color,
                    6,
                    2.5
                );

                bullets.splice(i,1);

                if(e.hp<=0){

                    score+=
                        e.elite
                        ?150
                        :50;

                    if(
                        Math.random()<.65
                    ){

                        cores.push({

                            x:e.x,
                            y:e.y,

                            radius:7,

                            life:600
                        });
                    }

                    createParticles(
                        e.x,
                        e.y,
                        e.color,
                        e.elite?25:14,
                        4
                    );

                    enemies.splice(j,1);
                }

                break;
            }
        }
    }
}


/* =========================
   ENEMIES UPDATE
========================= */

function updateEnemies(dt){

    for(
        let i=enemies.length-1;
        i>=0;
        i--
    ){

        const e=enemies[i];

        const angle=
            Math.atan2(
                player.y-e.y,
                player.x-e.x
            );

        e.x+=
            Math.cos(angle)*
            e.speed;

        e.y+=
            Math.sin(angle)*
            e.speed;

        const dist=
            Math.hypot(
                player.x-e.x,
                player.y-e.y
            );

        if(
            dist<
            player.radius+e.radius
        ){

            let damage=
                e.damage*dt/1000;

            if(player.shield>0){

                player.shield-=damage;

                if(player.shield<0){

                    player.hp+=
                        player.shield;

                    player.shield=0;
                }

            }else{

                player.hp-=damage;
            }

            const push=.7;

            e.x-=
                Math.cos(angle)*
                push;

            e.y-=
                Math.sin(angle)*
                push;

            if(player.hp<=0){

                endGame();

                return;
            }
        }
    }
}


/* =========================
   CORES
========================= */

function updateCores(){

    for(
        let i=cores.length-1;
        i>=0;
        i--
    ){

        const c=cores[i];

        c.life--;

        const dist=
            Math.hypot(
                player.x-c.x,
                player.y-c.y
            );

        if(
            dist<
            player.magnetRange
        ){

            const angle=
                Math.atan2(
                    player.y-c.y,
                    player.x-c.x
                );

            c.x+=
                Math.cos(angle)*4;

            c.y+=
                Math.sin(angle)*4;
        }

        if(
            dist<
            player.radius+c.radius
        ){

            xp+=25;

            cores.splice(i,1);

            createParticles(
                player.x,
                player.y,
                "#a855f7",
                12,
                3
            );

            if(xp>=xpNeeded){

                xp-=xpNeeded;

                xpNeeded=
                    Math.floor(
                        xpNeeded*1.3
                    );

                levelUp();
            }

            continue;
        }

        if(c.life<=0){

            cores.splice(i,1);
        }
    }
}


/* =========================
   WAVE SYSTEM
========================= */

function updateWave(dt){

    spawnTimer+=dt;
    waveTimer+=dt;

    const interval=
        Math.max(
            250,
            950-wave*35
        );

    if(
        spawnTimer>=interval
    ){

        spawnTimer=0;

        const amount=
            Math.random()<.12
            ?2
            :1;

        for(
            let i=0;
            i<amount;
            i++
        ){
            spawnEnemy();
        }
    }

    if(
        waveTimer>=30000
    ){

        waveTimer=0;

        wave++;

        if(wave>=FINAL_WAVE){

            winGame();

            return;
        }

        showToast(
            "WAVE "+wave
        );

        createParticles(
            player.x,
            player.y,
            "#00eaff",
            40,
            6
        );
    }
}


/* =========================
   LEVEL UP
========================= */

function levelUp(){

    paused=true;

    showRandomUpgrades();
}


/* =========================
   UPGRADE APPLY
========================= */

document
.querySelectorAll(".card")
.forEach(card=>{

    card.addEventListener(
        "click",
        ()=>{

            const type=
                card.dataset.upgrade;

            if(type==="fire"){

                player.fireRate=
                    Math.max(
                        65,
                        player.fireRate-25
                    );

                showToast(
                    "RAPID FIRE +"
                );
            }

            if(type==="power"){

                player.damage+=12;

                showToast(
                    "POWER SHOT +"
                );
            }

            if(type==="repair"){

                player.maxHp+=5;

                player.hp=
                    Math.min(
                        player.maxHp,
                        player.hp+30
                    );

                showToast(
                    "NANO REPAIR +"
                );
            }

            if(type==="speed"){

                player.speed+=.35;

                showToast(
                    "OVERDRIVE +"
                );
            }

            if(type==="magnet"){

                player.magnetRange+=55;

                showToast(
                    "CORE MAGNET +"
                );
            }

            if(type==="critical"){

                player.criticalChance=
                    Math.min(
                        .65,
                        player.criticalChance+.08
                    );

                showToast(
                    "CRITICAL CORE +"
                );
            }

            if(type==="shield"){

                player.shield+=40;

                showToast(
                    "ENERGY SHIELD +"
                );
            }

            if(type==="multi"){

                player.multishot=
                    Math.min(
                        5,
                        player.multishot+1
                    );

                showToast(
                    "MULTI SHOT +"
                );
            }

            upgradeScreen
                .classList
                .add("hidden");

            paused=false;

            updateUI();
        }
    );
});


/* =========================
   PARTICLES
========================= */

function createParticles(
    x,
    y,
    color,
    amount,
    speed
){

    for(
        let i=0;
        i<amount;
        i++
    ){

        const angle=
            Math.random()*
            Math.PI*2;

        const velocity=
            Math.random()*speed;

        particles.push({

            x,
            y,

            vx:
                Math.cos(angle)*
                velocity,

            vy:
                Math.sin(angle)*
                velocity,

            life:
                30+
                Math.random()*35,

            maxLife:65,

            size:
                1+
                Math.random()*3,

            color
        });
    }
}


function updateParticles(){

    for(
        let i=particles.length-1;
        i>=0;
        i--
    ){

        const p=particles[i];

        p.x+=p.vx;
        p.y+=p.vy;

        p.vx*=.96;
        p.vy*=.96;

        p.life--;

        if(p.life<=0){

            particles.splice(i,1);
        }
    }
}


/* =========================
   DRAW BACKGROUND
========================= */

function drawBackground(){

    ctx.fillStyle="#03050b";

    ctx.fillRect(
        0,
        0,
        W,
        H
    );

    const grid=50;

    ctx.strokeStyle=
        "rgba(0,180,255,.055)";

    ctx.lineWidth=1;

    const offset=
        (performance.now()/40)%grid;

    for(
        let x=-grid+offset;
        x<W;
        x+=grid
    ){

        ctx.beginPath();

        ctx.moveTo(x,0);

        ctx.lineTo(x,H);

        ctx.stroke();
    }

    for(
        let y=-grid+offset;
        y<H;
        y+=grid
    ){

        ctx.beginPath();

        ctx.moveTo(0,y);

        ctx.lineTo(W,y);

        ctx.stroke();
    }

    const gradient=
        ctx.createRadialGradient(
            W/2,
            H/2,
            100,
            W/2,
            H/2,
            Math.max(W,H)*.7
        );

    gradient.addColorStop(
        0,
        "rgba(0,180,255,.035)"
    );

    gradient.addColorStop(
        1,
        "rgba(0,0,0,.5)"
    );

    ctx.fillStyle=gradient;

    ctx.fillRect(
        0,
        0,
        W,
        H
    );
}


/* =========================
   DRAW PLAYER
========================= */

function drawPlayer(){

    const angle=
        Math.atan2(
            mouse.y-player.y,
            mouse.x-player.x
        );

    ctx.save();

    ctx.translate(
        player.x,
        player.y
    );

    ctx.rotate(angle);

    ctx.shadowBlur=25;
    ctx.shadowColor="#00eaff";

    ctx.fillStyle="#071b29";

    ctx.beginPath();

    ctx.moveTo(23,0);

    ctx.lineTo(-14,-13);

    ctx.lineTo(-8,0);

    ctx.lineTo(-14,13);

    ctx.closePath();

    ctx.fill();

    ctx.strokeStyle="#00eaff";

    ctx.lineWidth=2;

    ctx.stroke();

    ctx.fillStyle="#00eaff";

    ctx.beginPath();

    ctx.arc(
        4,
        0,
        5,
        0,
        Math.PI*2
    );

    ctx.fill();

    ctx.restore();

    ctx.shadowBlur=0;
}


/* =========================
   DRAW BULLETS
========================= */

function drawBullets(){

    for(const b of bullets){

        ctx.shadowBlur=15;

        ctx.shadowColor="#00eaff";

        ctx.fillStyle="#8df7ff";

        ctx.beginPath();

        ctx.arc(
            b.x,
            b.y,
            b.radius,
            0,
            Math.PI*2
        );

        ctx.fill();
    }

    ctx.shadowBlur=0;
}


/* =========================
   DRAW ENEMIES
========================= */

function drawEnemies(){

    for(const e of enemies){

        ctx.save();

        ctx.translate(
            e.x,
            e.y
        );

        ctx.shadowBlur=
            e.elite
            ?25
            :15;

        ctx.shadowColor=e.color;

        ctx.fillStyle=
            e.elite
            ?"#36091c"
            :"#260b12";

        ctx.beginPath();

        ctx.arc(
            0,
            0,
            e.radius,
            0,
            Math.PI*2
        );

        ctx.fill();

        ctx.strokeStyle=e.color;

        ctx.lineWidth=
            e.elite
            ?3
            :2;

        ctx.stroke();

        ctx.fillStyle=e.color;

        ctx.beginPath();

        ctx.arc(
            0,
            0,
            e.radius*.3,
            0,
            Math.PI*2
        );

        ctx.fill();

        ctx.restore();

        ctx.shadowBlur=0;

        const barWidth=
            e.radius*2;

        ctx.fillStyle=
            "rgba(0,0,0,.5)";

        ctx.fillRect(
            e.x-barWidth/2,
            e.y-e.radius-10,
            barWidth,
            4
        );

        ctx.fillStyle=e.color;

        ctx.fillRect(
            e.x-barWidth/2,
            e.y-e.radius-10,
            barWidth*
            (e.hp/e.maxHp),
            4
        );
    }
}


/* =========================
   DRAW CORES
========================= */

function drawCores(){

    for(const c of cores){

        const pulse=
            1+
            Math.sin(
                performance.now()/100
            )*.15;

        ctx.save();

        ctx.translate(
            c.x,
            c.y
        );

        ctx.scale(
            pulse,
            pulse
        );

        ctx.shadowBlur=20;

        ctx.shadowColor="#a855f7";

        ctx.fillStyle="#c084fc";

        ctx.rotate(
            performance.now()/500
        );

        ctx.beginPath();

        ctx.moveTo(0,-8);

        ctx.lineTo(8,0);

        ctx.lineTo(0,8);

        ctx.lineTo(-8,0);

        ctx.closePath();

        ctx.fill();

        ctx.restore();
    }

    ctx.shadowBlur=0;
}


/* =========================
   DRAW PARTICLES
========================= */

function drawParticles(){

    for(const p of particles){

        ctx.globalAlpha=
            Math.max(
                0,
                p.life/p.maxLife
            );

        ctx.fillStyle=p.color;

        ctx.fillRect(
            p.x,
            p.y,
            p.size,
            p.size
        );
    }

    ctx.globalAlpha=1;
}


/* =========================
   UI
========================= */

function updateUI(){

    hpText.textContent=
        Math.max(
            0,
            Math.floor(player.hp)
        );

    hpBar.style.width=
        Math.max(
            0,
            player.hp/
            player.maxHp*
            100
        )+"%";

    xpBar.style.width=
        Math.min(
            100,
            xp/
            xpNeeded*
            100
        )+"%";

    scoreText.textContent=
        Math.floor(score);

    waveText.textContent=wave;

    bestText.textContent=bestScore;
}


/* =========================
   GAME OVER
========================= */

function endGame(){

    if(gameOver || victory){
        return;
    }

    gameOver=true;
    running=false;

    if(score>bestScore){

        bestScore=
            Math.floor(score);

        localStorage.setItem(
            "neonCoreBest",
            bestScore
        );
    }

    document.getElementById(
        "finalScore"
    ).textContent=
        Math.floor(score);

    document.getElementById(
        "finalWave"
    ).textContent=wave;

    document.getElementById(
        "finalBest"
    ).textContent=bestScore;

    gameOverScreen
        .classList
        .remove("hidden");

    createParticles(
        player.x,
        player.y,
        "#ff164f",
        80,
        8
    );

    updateUI();
}


/* =========================
   VICTORY
========================= */

function winGame(){

    if(gameOver || victory){
        return;
    }

    victory=true;
    running=false;
    paused=false;

    score+=5000;

    if(score>bestScore){

        bestScore=
            Math.floor(score);

        localStorage.setItem(
            "neonCoreBest",
            bestScore
        );
    }

    document.getElementById(
        "victoryScore"
    ).textContent=
        Math.floor(score);

    document.getElementById(
        "victoryBest"
    ).textContent=
        bestScore;

    victoryScreen
        .classList
        .remove("hidden");

    createParticles(
        player.x,
        player.y,
        "#00eaff",
        150,
        10
    );

    updateUI();
}


/* =========================
   TOAST
========================= */

let toastTimer;

function showToast(text){

    const toast=
        document.getElementById(
            "toast"
        );

    toast.textContent=text;

    toast.style.opacity=1;

    clearTimeout(toastTimer);

    toastTimer=
        setTimeout(
            ()=>{
                toast.style.opacity=0;
            },
            1200
        );
}


/* =========================
   MAIN LOOP
========================= */

let lastTime=
    performance.now();

function loop(now){

    const dt=
        Math.min(
            40,
            now-lastTime
        );

    lastTime=now;

    drawBackground();

    if(
        running &&
        !paused
    ){

        updatePlayer();

        updateBullets();

        updateEnemies(dt);

        updateCores();

        updateParticles();

        updateWave(dt);

        updateUI();

    }else{

        updateParticles();
    }

    drawCores();

    drawBullets();

    drawEnemies();

    drawPlayer();

    drawParticles();

    requestAnimationFrame(loop);
}

requestAnimationFrame(loop);


/* ============================================================
   RANK UP ONLINE MODE — ORIGINAL NEON CORE CLIENT
   Preserves the original visual/game renderer and replaces only
   local simulation with the authoritative RANK UP WebSocket state.
============================================================ */

const ONLINE_ARENA_WIDTH = 1100;
const ONLINE_ARENA_HEIGHT = 700;

const onlineParams = new URLSearchParams(window.location.search);
const launchTicket = onlineParams.get("ticket");

let onlineSocket = null;
let onlineConnected = false;
let onlineAuthenticated = false;
let onlineRoomCode = "";
let onlineMySlot = null;
let onlineState = null;
let onlineRemotePlayers = [];
let onlineLastEnemyIds = new Set();
let onlineCosmeticCores = [];
let onlineRemoteAngle = 0;
let onlineStatusMessage = "CONNECTING TO RANK UP...";
let onlineRoomStarted = false;
let onlineLastStatus = "";

const onlineConnectionEl =
    document.getElementById("onlineConnection");

const createRoomButton =
    document.getElementById("createRoomButton");

const joinRoomButton =
    document.getElementById("joinRoomButton");

const roomCodeInput =
    document.getElementById("roomCodeInput");

const roomCodeDisplay =
    document.getElementById("roomCodeDisplay");

const originalStartButton =
    document.getElementById("startButton");

const onlineEnterButton =
    document.getElementById("startButtonOnline");

const remotePlayerLabel =
    document.getElementById("remotePlayerLabel");

function setOnlineStatus(text, error=false){
    onlineStatusMessage = text || "";
    if(onlineConnectionEl){
        onlineConnectionEl.textContent = onlineStatusMessage;
        onlineConnectionEl.classList.toggle("error", !!error);
    }
}

function wsUrlForTicket(ticket){
    const scheme =
        location.protocol === "https:"
        ? "wss:"
        : "ws:";

    return (
        scheme +
        "//" +
        location.host +
        "/ws/neon/" +
        encodeURIComponent(ticket)
    );
}

function onlineScaleX(x){
    return (
        Number(x || 0) *
        (W / ONLINE_ARENA_WIDTH)
    );
}

function onlineScaleY(y){
    return (
        Number(y || 0) *
        (H / ONLINE_ARENA_HEIGHT)
    );
}

function normalizeServerEnemy(enemy){
    const kind = String(
        enemy.kind || "normal"
    ).toLowerCase();

    const elite =
        kind === "elite" ||
        kind === "boss";

    let color = "#ff3b30";

    if(kind === "elite"){
        color = "#ff2d78";
    }

    if(kind === "boss"){
        color = "#ff00d4";
    }

    return {
        id: enemy.id,
        x: onlineScaleX(enemy.x),
        y: onlineScaleY(enemy.y),
        radius:
            Math.max(
                8,
                Number(enemy.radius || 15) *
                Math.min(
                    W / ONLINE_ARENA_WIDTH,
                    H / ONLINE_ARENA_HEIGHT
                )
            ),
        hp: Number(enemy.hp || 0),
        maxHp: Number(enemy.max_hp || 1),
        elite,
        kind,
        color
    };
}

function normalizeServerBullet(bullet){
    return {
        x: onlineScaleX(bullet.x),
        y: onlineScaleY(bullet.y),
        radius: 5,
        life: 2,
        vx: 0,
        vy: 0,
        damage: 0
    };
}

function snapshotEnemyIds(){
    const set = new Set();
    for(const enemy of (enemies || [])){
        set.add(String(enemy.id));
    }
    return set;
}

function spawnCosmeticDeathCore(x,y,color){
    onlineCosmeticCores.push({
        x:x,
        y:y,
        radius:7,
        life:60,
        color:color || "#a855f7"
    });
}

function applyOnlineState(state){
    onlineState = state;

    const serverPlayers =
        Array.isArray(state.players)
        ? state.players
        : [];

    const me =
        serverPlayers.find(
            p => Number(p.slot) === Number(onlineMySlot)
        ) ||
        serverPlayers[0] ||
        null;

    onlineRemotePlayers =
        serverPlayers.filter(
            p => Number(p.slot) !== Number(onlineMySlot)
        );

    if(me){
        player.x = onlineScaleX(me.x);
        player.y = onlineScaleY(me.y);

        const sx =
            W / ONLINE_ARENA_WIDTH;

        const sy =
            H / ONLINE_ARENA_HEIGHT;

        player.radius =
            17 * Math.min(sx,sy);

        player.hp =
            Number(me.hp || 0);

        player.maxHp =
            Number(me.max_hp || 100);

        score =
            Number(me.score || 0);

        xp =
            Number(me.xp || 0);

        wave =
            Number(state.wave || 1);

        if(me.level != null){
            window.onlinePlayerLevel =
                Number(me.level);
        }

        if(
            me.downed ||
            me.eliminated
        ){
            player.hp =
                Math.max(
                    0,
                    Number(me.hp || 0)
                );
        }

        if(
            me.pending_upgrade
        ){
            showOnlineUpgrade(
                me.pending_upgrade
            );
        }else{
            upgradeScreen.classList.add(
                "hidden"
            );
            paused = false;
        }
    }

    const oldIds = onlineLastEnemyIds;

    const nextEnemies =
        Array.isArray(state.enemies)
        ? state.enemies.map(
            normalizeServerEnemy
        )
        : [];

    const nextIds = new Set(
        nextEnemies.map(
            e => String(e.id)
        )
    );

    for(const oldEnemy of (enemies || [])){
        const id = String(oldEnemy.id);

        if(!nextIds.has(id)){
            spawnCosmeticDeathCore(
                oldEnemy.x,
                oldEnemy.y,
                oldEnemy.color
            );

            createParticles(
                oldEnemy.x,
                oldEnemy.y,
                oldEnemy.color,
                oldEnemy.kind === "boss" ? 40 : 14,
                oldEnemy.kind === "boss" ? 6 : 4
            );
        }
    }

    enemies = nextEnemies;

    bullets =
        Array.isArray(state.bullets)
        ? state.bullets.map(
            normalizeServerBullet
        )
        : [];

    onlineLastEnemyIds = nextIds;

    wave = Number(
        state.wave || 1
    );

    onlineRoomStarted =
        state.status === "playing";

    if(
        state.wave_message &&
        state.wave_message !== onlineLastStatus
    ){
        showToast(
            state.wave_message
        );
        onlineLastStatus =
            state.wave_message;
    }

    if(state.status === "won"){
        onlineShowVictory();
    }

    if(state.status === "lost"){
        onlineShowGameOver();
    }

    updateUI();
}

function openOnlineArena(){
    startScreen.classList.add(
        "hidden"
    );
    gameOverScreen.classList.add(
        "hidden"
    );
    victoryScreen.classList.add(
        "hidden"
    );

    running = true;
    paused = false;
    gameOver = false;
    victory = false;
}

function handleOnlineAuth(message){
    onlineAuthenticated = true;
    setOnlineStatus(
        "CONNECTED AS " +
        String(message.name || "RANK UP").toUpperCase()
    );

    createRoomButton.disabled = false;
    joinRoomButton.disabled = false;
    roomCodeInput.disabled = false;
}

function handleOnlineMessage(message){
    if(!message){
        return;
    }

    if(message.type === "authenticated"){
        handleOnlineAuth(message);
        return;
    }

    if(message.type === "room_created"){
        onlineRoomCode =
            String(
                message.room_code || ""
            ).toUpperCase();

        roomCodeDisplay.innerHTML =
            "ROOM CODE: <strong>" +
            onlineRoomCode +
            "</strong>";

        setOnlineStatus(
            "WAITING FOR PLAYER 2..."
        );

        onlineEnterButton.style.display =
            "none";

        return;
    }

    if(message.type === "joined"){
        onlineRoomCode =
            String(
                message.room_code || onlineRoomCode
            ).toUpperCase();

        onlineMySlot =
            Number(message.slot || 1);

        roomCodeDisplay.innerHTML =
            "ROOM CODE: <strong>" +
            onlineRoomCode +
            "</strong> • PLAYER " +
            onlineMySlot;

        setOnlineStatus(
            "JOINED ROOM " +
            onlineRoomCode
        );

        return;
    }

    if(message.type === "state"){
        if(
            onlineMySlot == null &&
            Array.isArray(message.players) &&
            message.players.length
        ){
            onlineMySlot =
                Number(
                    message.players[0].slot
                );
        }

        if(message.status === "waiting"){
            setOnlineStatus(
                "WAITING FOR PLAYER 2..."
            );
        }else if(
            message.status === "playing"
        ){
            setOnlineStatus(
                "ONLINE • TWO PLAYER CO-OP"
            );

            if(!onlineRoomStarted){
                openOnlineArena();
            }
        }

        applyOnlineState(message);
        return;
    }

    if(message.type === "error"){
        setOnlineStatus(
            String(
                message.message ||
                "ONLINE ERROR"
            ),
            true
        );

        if(
            message.message === "Room is full."
        ){
            joinRoomButton.disabled = false;
        }

        return;
    }

    if(message.type === "pong"){
        return;
    }
}

function connectOnline(){
    if(!launchTicket){
        setOnlineStatus(
            "MISSING RANK UP LAUNCH TICKET",
            true
        );

        createRoomButton.disabled = true;
        joinRoomButton.disabled = true;
        roomCodeInput.disabled = true;

        return;
    }

    setOnlineStatus(
        "CONNECTING TO RANK UP..."
    );

    try{
        onlineSocket =
            new WebSocket(
                wsUrlForTicket(
                    launchTicket
                )
            );
    }catch(error){
        setOnlineStatus(
            "WEBSOCKET START FAILED",
            true
        );
        return;
    }

    onlineSocket.onopen = ()=>{
        onlineConnected = true;

        setOnlineStatus(
            "CONNECTED • AUTHENTICATING..."
        );
    };

    onlineSocket.onmessage = event=>{
        try{
            const message =
                JSON.parse(
                    event.data
                );

            handleOnlineMessage(
                message
            );
        }catch(error){
            setOnlineStatus(
                "INVALID SERVER DATA",
                true
            );
        }
    };

    onlineSocket.onerror = ()=>{
        setOnlineStatus(
            "ONLINE CONNECTION ERROR",
            true
        );
    };

    onlineSocket.onclose = ()=>{
        onlineConnected = false;

        if(
            !gameOver &&
            !victory
        ){
            setOnlineStatus(
                "SERVER CONNECTION CLOSED",
                true
            );
        }
    };
}

function sendOnlineMessage(payload){
    if(
        !onlineSocket ||
        onlineSocket.readyState !== WebSocket.OPEN
    ){
        return false;
    }

    try{
        onlineSocket.send(
            JSON.stringify(payload)
        );

        return true;
    }catch(error){
        return false;
    }
}

createRoomButton.onclick = ()=>{
    if(!onlineAuthenticated){
        return;
    }

    setOnlineStatus(
        "CREATING ROOM..."
    );

    createRoomButton.disabled = true;

    sendOnlineMessage({
        type:"create_room"
    });
};

joinRoomButton.onclick = ()=>{
    if(!onlineAuthenticated){
        return;
    }

    const code =
        roomCodeInput.value
        .trim()
        .toUpperCase();

    if(code.length !== 6){
        setOnlineStatus(
            "ENTER A 6-CHARACTER ROOM CODE",
            true
        );
        return;
    }

    setOnlineStatus(
        "JOINING ROOM " + code + "..."
    );

    joinRoomButton.disabled = true;

    sendOnlineMessage({
        type:"join_room",
        room_code:code
    });
};

roomCodeInput.addEventListener(
    "input",
    ()=>{
        roomCodeInput.value =
            roomCodeInput.value
            .replace(
                /[^a-zA-Z0-9]/g,
                ""
            )
            .toUpperCase()
            .slice(0,6);
    }
);

onlineEnterButton.onclick =
    ()=>{
        if(onlineRoomStarted){
            openOnlineArena();
        }
    };

originalStartButton.disabled = true;
originalStartButton.style.display = "none";

/* Keep the original restart/victory screens, but make them reconnect.
   The server remains authoritative, so these buttons reload the client.
*/
restartButton.onclick = ()=>{
    location.reload();
};

victoryButton.onclick = ()=>{
    location.reload();
};

function showOnlineUpgrade(choice){
    const normalized = [];

    if(Array.isArray(choice)){
        for(const value of choice){
            normalized.push(
                String(value)
            );
        }
    }else if(
        choice &&
        typeof choice === "object"
    ){
        for(const key of Object.keys(choice)){
            normalized.push(
                String(key)
            );
        }
    }else if(choice){
        normalized.push(
            String(choice)
        );
    }

    const fallback = [
        "damage",
        "speed",
        "fire"
    ];

    const options =
        normalized.length
        ? normalized
        : fallback;

    const unique =
        [...new Set(options)];

    const finalChoices =
        unique.length >= 3
        ? unique.slice(0,3)
        : [...unique, ...fallback]
            .filter(
                (v,i,a)=>
                    a.indexOf(v) === i
            )
            .slice(0,3);

    const titleMap = {
        damage:"POWER SHOT",
        speed:"OVERDRIVE",
        fire:"RAPID FIRE",
        health:"NANO REPAIR"
    };

    const textMap = {
        damage:"Increase weapon damage.",
        speed:"Increase movement speed.",
        fire:"Increase weapon fire rate.",
        health:"Restore and increase HP."
    };

    document.querySelectorAll(
        ".card"
    ).forEach(
        (card,index)=>{
            if(index >= finalChoices.length){
                return;
            }

            const id =
                finalChoices[index];

            card.dataset.upgrade =
                id;

            const icon =
                card.querySelector(".icon");

            const heading =
                card.querySelector("h3");

            const paragraph =
                card.querySelector("p");

            if(icon){
                icon.textContent =
                    id === "damage"
                    ? "💥"
                    : id === "speed"
                    ? "🏃"
                    : id === "fire"
                    ? "⚡"
                    : "❤️";
            }

            if(heading){
                heading.textContent =
                    titleMap[id] ||
                    String(id).toUpperCase();
            }

            if(paragraph){
                paragraph.textContent =
                    textMap[id] ||
                    "Choose this upgrade.";
            }
        }
    );

    upgradeScreen.classList.remove(
        "hidden"
    );

    paused = true;
}

document.querySelectorAll(
    ".card"
).forEach(
    card=>{
        card.addEventListener(
            "click",
            ()=>{
                if(!onlineRoomStarted){
                    return;
                }

                const type =
                    card.dataset.upgrade;

                sendOnlineMessage({
                    type:"upgrade",
                    choice:type
                });

                upgradeScreen.classList.add(
                    "hidden"
                );

                paused = false;
            }
        );
    }
);

/* Override the original local update functions by making the
   online loop render server state only. */
function onlineUpdateVisualCores(){
    for(
        let i=onlineCosmeticCores.length-1;
        i>=0;
        i--
    ){
        const core =
            onlineCosmeticCores[i];

        core.life -= 1;

        if(core.life <= 0){
            onlineCosmeticCores.splice(
                i,
                1
            );
        }
    }
}

function onlineDrawRemotePlayers(){
    for(
        const remote of onlineRemotePlayers
    ){
        const x =
            onlineScaleX(remote.x);

        const y =
            onlineScaleY(remote.y);

        const sx =
            W / ONLINE_ARENA_WIDTH;

        const sy =
            H / ONLINE_ARENA_HEIGHT;

        const radius =
            17 * Math.min(
                sx,
                sy
            );

        ctx.save();

        ctx.translate(
            x,
            y
        );

        ctx.rotate(
            onlineRemoteAngle
        );

        ctx.shadowBlur = 25;
        ctx.shadowColor =
            remote.downed
            ? "#ff164f"
            : "#d05cff";

        ctx.fillStyle =
            remote.downed
            ? "#350915"
            : "#170b2b";

        ctx.beginPath();

        ctx.moveTo(
            23 * Math.min(sx,sy),
            0
        );

        ctx.lineTo(
            -14 * Math.min(sx,sy),
            -13 * Math.min(sx,sy)
        );

        ctx.lineTo(
            -8 * Math.min(sx,sy),
            0
        );

        ctx.lineTo(
            -14 * Math.min(sx,sy),
            13 * Math.min(sx,sy)
        );

        ctx.closePath();

        ctx.fill();

        ctx.strokeStyle =
            remote.downed
            ? "#ff164f"
            : "#d05cff";

        ctx.lineWidth = 2;
        ctx.stroke();

        ctx.fillStyle =
            remote.downed
            ? "#ff164f"
            : "#d05cff";

        ctx.beginPath();

        ctx.arc(
            4 * Math.min(sx,sy),
            0,
            5 * Math.min(sx,sy),
            0,
            Math.PI*2
        );

        ctx.fill();

        ctx.restore();

        const label =
            remote.downed
            ? "DOWNED"
            : String(
                remote.name ||
                "PLAYER 2"
            );

        ctx.save();

        ctx.font =
            "bold 11px Arial";

        ctx.textAlign =
            "center";

        ctx.fillStyle =
            remote.downed
            ? "#ff6685"
            : "#ff7cf3";

        ctx.shadowBlur = 10;
        ctx.shadowColor =
            remote.downed
            ? "#ff164f"
            : "#d05cff";

        ctx.fillText(
            label,
            x,
            y - radius - 12
        );

        ctx.restore();
    }
}

function onlineDrawCosmeticCores(){
    for(
        const c of onlineCosmeticCores
    ){
        const pulse =
            1 +
            Math.sin(
                performance.now()/100
            ) * .15;

        ctx.save();

        ctx.translate(
            c.x,
            c.y
        );

        ctx.scale(
            pulse,
            pulse
        );

        ctx.shadowBlur = 20;
        ctx.shadowColor =
            "#a855f7";

        ctx.fillStyle =
            "#c084fc";

        ctx.rotate(
            performance.now()/500
        );

        ctx.beginPath();

        ctx.moveTo(
            0,-8
        );

        ctx.lineTo(
            8,0
        );

        ctx.lineTo(
            0,8
        );

        ctx.lineTo(
            -8,0
        );

        ctx.closePath();

        ctx.fill();

        ctx.restore();
    }

    ctx.shadowBlur = 0;
}

function onlineSendInput(){
    if(!onlineRoomStarted){
        return;
    }

    const angle =
        Math.atan2(
            mouse.y - player.y,
            mouse.x - player.x
        );

    sendOnlineMessage({
        type:"input",
        data:{
            up:
                !!(
                    keys["w"] ||
                    keys["arrowup"]
                ),
            down:
                !!(
                    keys["s"] ||
                    keys["arrowdown"]
                ),
            left:
                !!(
                    keys["a"] ||
                    keys["arrowleft"]
                ),
            right:
                !!(
                    keys["d"] ||
                    keys["arrowright"]
                ),
            shoot:
                !!mouse.down,
            revive:
                !!keys["e"],
            angle:angle
        }
    });
}

setInterval(
    onlineSendInput,
    50
);

function onlineShowVictory(){
    if(victory){
        return;
    }

    running = false;
    paused = false;
    victory = true;

    gameOverScreen.classList.add(
        "hidden"
    );

    victoryScreen.classList.remove(
        "hidden"
    );

    const finalScore =
        onlineState &&
        Array.isArray(
            onlineState.players
        )
        ? onlineState.players.reduce(
            (sum,p)=>
                sum +
                Number(p.score || 0),
            0
        )
        : score;

    document.getElementById(
        "victoryScore"
    ).textContent =
        Math.floor(
            finalScore
        );

    document.getElementById(
        "victoryBest"
    ).textContent =
        Math.max(
            Number(
                bestScore || 0
            ),
            Math.floor(
                finalScore
            )
        );
}

function onlineShowGameOver(){
    if(gameOver){
        return;
    }

    running = false;
    paused = false;
    gameOver = true;

    gameOverScreen.classList.remove(
        "hidden"
    );

    const finalScore =
        onlineState &&
        Array.isArray(
            onlineState.players
        )
        ? onlineState.players.reduce(
            (sum,p)=>
                sum +
                Number(p.score || 0),
            0
        )
        : score;

    document.getElementById(
        "finalScore"
    ).textContent =
        Math.floor(
            finalScore
        );

    document.getElementById(
        "finalWave"
    ).textContent =
        Number(
            wave || 1
        );

    document.getElementById(
        "finalBest"
    ).textContent =
        Math.max(
            Number(
                bestScore || 0
            ),
            Math.floor(
                finalScore
            )
        );
}

/* Replace the original loop with an online, server-authoritative loop. */
function onlineLoop(now){
    const dt =
        Math.min(
            40,
            now - lastTime
        );

    lastTime = now;

    drawBackground();

    if(onlineRoomStarted){
        updateParticles();
        onlineUpdateVisualCores();
    }else{
        updateParticles();
    }

    drawCores();
    onlineDrawCosmeticCores();
    drawBullets();
    drawEnemies();
    drawPlayer();
    onlineDrawRemotePlayers();
    drawParticles();

    requestAnimationFrame(
        onlineLoop
    );
}

cancelAnimationFrame(
    window.__neonOriginalFrame ||
    0
);

requestAnimationFrame(
    onlineLoop
);

/* Start online connection after the original file has finished
   defining all renderer functions and DOM elements. */
connectOnline();

</script>

</body>
</html>"""


@app.get("/neon", response_class=HTMLResponse)
def neon_client():
    return HTMLResponse(
        content=NEON_HTML
    )

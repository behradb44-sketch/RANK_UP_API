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

NEON_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>NEON CORE</title>

<style>
*{
    box-sizing:border-box;
}

html,body{
    margin:0;
    width:100%;
    height:100%;
    overflow:hidden;
    background:#030711;
    color:#eaf4ff;
    font-family:Arial,Helvetica,sans-serif;
}

body{
    display:flex;
    align-items:center;
    justify-content:center;
}

#app{
    width:100%;
    height:100%;
    position:relative;
}

canvas{
    position:absolute;
    inset:0;
    width:100%;
    height:100%;
    background:#030711;
}

#menu{
    position:absolute;
    inset:0;
    display:flex;
    align-items:center;
    justify-content:center;
    background:
        radial-gradient(
            circle at center,
            #101d38 0%,
            #030711 65%
        );
    z-index:10;
}

.panel{
    width:min(470px,92vw);
    padding:32px;
    border:1px solid #23456d;
    border-radius:22px;
    background:rgba(5,12,27,.94);
    box-shadow:
        0 0 50px rgba(0,140,255,.15);
    text-align:center;
}

.logo{
    font-size:42px;
    font-weight:900;
    letter-spacing:5px;
    margin-bottom:5px;
}

.logo span{
    color:#25a7ff;
}

.subtitle{
    color:#8ba5c4;
    margin-bottom:28px;
}

button,input{
    width:100%;
    height:50px;
    border-radius:12px;
    border:1px solid #315477;
    font-size:16px;
}

button{
    cursor:pointer;
    background:#0e7cff;
    color:white;
    font-weight:bold;
    margin-top:10px;
}

button:hover{
    background:#2690ff;
}

button.secondary{
    background:#0b1426;
}

input{
    background:#071020;
    color:white;
    padding:0 15px;
    outline:none;
    text-transform:uppercase;
    text-align:center;
    letter-spacing:4px;
}

#roomCode{
    font-size:24px;
    margin:12px 0;
}

#status{
    min-height:24px;
    color:#8fb4d9;
    margin-top:15px;
}

#hud{
    display:none;
    position:absolute;
    inset:0;
    pointer-events:none;
    z-index:5;
}

.topbar{
    position:absolute;
    top:15px;
    left:15px;
    right:15px;
    display:flex;
    justify-content:space-between;
    gap:12px;
}

.card{
    background:rgba(3,9,20,.82);
    border:1px solid #1e4268;
    border-radius:14px;
    padding:10px 14px;
    backdrop-filter:blur(8px);
}

.playerStats{
    min-width:230px;
}

.bar{
    height:8px;
    background:#172437;
    border-radius:10px;
    overflow:hidden;
    margin-top:5px;
}

.hp{
    height:100%;
    background:#21d98a;
}

.xp{
    height:100%;
    background:#2a9cff;
}

#centerMessage{
    position:absolute;
    top:45%;
    left:50%;
    transform:translate(-50%,-50%);
    font-size:54px;
    font-weight:900;
    letter-spacing:6px;
    text-shadow:0 0 25px #1598ff;
    text-align:center;
}

#help{
    position:absolute;
    bottom:14px;
    left:50%;
    transform:translateX(-50%);
    color:#7890ab;
    font-size:13px;
    text-align:center;
}

#upgrade{
    display:none;
    position:absolute;
    inset:0;
    align-items:center;
    justify-content:center;
    background:rgba(0,0,0,.65);
    pointer-events:auto;
    z-index:20;
}

.upgradePanel{
    width:min(650px,92vw);
    background:#07101f;
    border:1px solid #23639b;
    border-radius:20px;
    padding:25px;
    text-align:center;
}

.upgradePanel h2{
    margin-top:0;
}

.options{
    display:grid;
    grid-template-columns:repeat(3,1fr);
    gap:12px;
}

.option{
    min-height:150px;
    background:#0a172b;
    border:1px solid #254a70;
    border-radius:15px;
    padding:15px;
    cursor:pointer;
}

.option:hover{
    border-color:#2199ff;
    transform:translateY(-2px);
}

.option h3{
    color:#39aaff;
}

#result{
    display:none;
    position:absolute;
    inset:0;
    z-index:30;
    align-items:center;
    justify-content:center;
    background:rgba(1,5,12,.9);
}

.resultTitle{
    font-size:65px;
    font-weight:900;
    letter-spacing:5px;
}

.resultScore{
    font-size:28px;
    margin:15px 0;
}

@media(max-width:650px){
    .options{
        grid-template-columns:1fr;
    }

    .resultTitle{
        font-size:42px;
    }
}
</style>
</head>

<body>

<div id="app">

<canvas id="game"></canvas>

<div id="menu">

    <div class="panel">

        <div class="logo">
            NEON <span>CORE</span>
        </div>

        <div class="subtitle">
            RANK UP • TWO PLAYER CO-OP
        </div>

        <button id="createBtn">
            CREATE ROOM
        </button>

        <div style="margin:18px 0;color:#526b87;">
            OR
        </div>

        <input
            id="roomInput"
            maxlength="6"
            placeholder="ROOM CODE"
        >

        <button
            id="joinBtn"
            class="secondary"
        >
            JOIN ROOM
        </button>

        <div id="status">
            Connecting to RANK UP...
        </div>

    </div>

</div>

<div id="hud">

    <div class="topbar">

        <div
            class="card playerStats"
            id="p1"
        ></div>

        <div
            class="card"
            id="waveInfo"
        >
            WAVE 0
        </div>

        <div
            class="card playerStats"
            id="p2"
        ></div>

    </div>

    <div id="centerMessage"></div>

    <div id="help">
        WASD / ARROWS = MOVE • MOUSE = AIM • LEFT CLICK = SHOOT • E = REVIVE
    </div>

</div>

<div id="upgrade">

    <div class="upgradePanel">

        <h2>LEVEL UP</h2>

        <p>
            Choose an upgrade
        </p>

        <div
            class="options"
            id="options"
        ></div>

    </div>

</div>

<div id="result">

    <div class="panel">

        <div
            id="resultTitle"
            class="resultTitle"
        >
            VICTORY
        </div>

        <div
            id="resultScore"
            class="resultScore"
        ></div>

        <button
            onclick="location.reload()"
        >
            PLAY AGAIN
        </button>

    </div>

</div>

</div>

<script>

const canvas = document.getElementById("game");
const ctx = canvas.getContext("2d");

let ws = null;
let state = null;

let mySlot = null;
let ticket = null;

let keys = {};
let mouseX = 0;
let mouseY = 0;
let shooting = false;
let reviving = false;

const menu = document.getElementById("menu");
const hud = document.getElementById("hud");
const statusBox = document.getElementById("status");
const upgrade = document.getElementById("upgrade");
const optionsBox = document.getElementById("options");
const result = document.getElementById("result");

ticket = new URLSearchParams(
    location.search
).get("ticket");

function resize(){
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
}

window.addEventListener(
    "resize",
    resize
);

resize();


function setStatus(text){
    statusBox.textContent = text;
}


function connect(){

    if(!ticket){
        setStatus(
            "Missing RANK UP launch ticket."
        );
        return;
    }

    const protocol =
        location.protocol === "https:"
        ? "wss:"
        : "ws:";

    ws = new WebSocket(
        protocol +
        "//" +
        location.host +
        "/ws/neon/" +
        encodeURIComponent(ticket)
    );

    ws.onopen = function(){

        setStatus(
            "Connected. Create a room or join one."
        );
    };

    ws.onmessage = function(event){

        const data = JSON.parse(
            event.data
        );

        if(data.type === "authenticated"){

            setStatus(
                "Logged in as " +
                data.name
            );

            return;
        }

        if(data.type === "room_created"){

            setStatus(
                "ROOM CODE: " +
                data.room_code +
                " — waiting for Player 2..."
            );

            return;
        }

        if(data.type === "joined"){

            mySlot = data.slot;

            menu.style.display = "none";
            hud.style.display = "block";

            return;
        }

        if(data.type === "error"){

            setStatus(
                data.message
            );

            return;
        }

        if(data.type === "state"){

            state = data;

            if(
                state.status === "playing" ||
                state.status === "won" ||
                state.status === "lost"
            ){

                menu.style.display = "none";
                hud.style.display = "block";
            }

            updateUpgrade();

            if(
                state.status === "won" ||
                state.status === "lost"
            ){
                showResult();
            }
        }
    };

    ws.onclose = function(){

        if(
            !state ||
            (
                state.status !== "won" &&
                state.status !== "lost"
            )
        ){

            setStatus(
                "Disconnected from RANK UP."
            );
        }
    };
}


document
    .getElementById("createBtn")
    .onclick = function(){

        if(!ws){
            return;
        }

        ws.send(
            JSON.stringify({
                type:"create_room"
            })
        );
    };


document
    .getElementById("joinBtn")
    .onclick = function(){

        if(!ws){
            return;
        }

        const code =
            document
            .getElementById("roomInput")
            .value
            .trim()
            .toUpperCase();

        if(code.length !== 6){

            setStatus(
                "Enter a 6 character room code."
            );

            return;
        }

        ws.send(
            JSON.stringify({
                type:"join_room",
                room_code:code
            })
        );
    };


window.addEventListener(
    "keydown",
    function(e){

        keys[e.key.toLowerCase()] = true;

        if(
            e.key.toLowerCase() === "e"
        ){
            reviving = true;
        }
    }
);


window.addEventListener(
    "keyup",
    function(e){

        keys[e.key.toLowerCase()] = false;

        if(
            e.key.toLowerCase() === "e"
        ){
            reviving = false;
        }
    }
);


canvas.addEventListener(
    "mousemove",
    function(e){

        mouseX = e.clientX;
        mouseY = e.clientY;
    }
);


canvas.addEventListener(
    "mousedown",
    function(){

        shooting = true;
    }
);


window.addEventListener(
    "mouseup",
    function(){

        shooting = false;
    }
);


function sendInput(){

    if(!ws){
        return;
    }

    if(ws.readyState !== WebSocket.OPEN){
        return;
    }

    if(!state){
        return;
    }

    const me =
        state.players.find(
            p => p.slot === mySlot
        );

    if(!me){
        return;
    }

    const sx =
        canvas.width /
        state.arena.width;

    const sy =
        canvas.height /
        state.arena.height;

    const worldMouseX =
        mouseX / sx;

    const worldMouseY =
        mouseY / sy;

    const angle =
        Math.atan2(
            worldMouseY - me.y,
            worldMouseX - me.x
        );

    ws.send(
        JSON.stringify({
            type:"input",

            data:{
                up:
                    keys["w"] ||
                    keys["arrowup"] ||
                    false,

                down:
                    keys["s"] ||
                    keys["arrowdown"] ||
                    false,

                left:
                    keys["a"] ||
                    keys["arrowleft"] ||
                    false,

                right:
                    keys["d"] ||
                    keys["arrowright"] ||
                    false,

                shoot:shooting,

                revive:reviving,

                angle:angle
            }
        })
    );
}


setInterval(
    sendInput,
    50
);


function worldToScreen(x,y){

    return {
        x:
            x /
            state.arena.width *
            canvas.width,

        y:
            y /
            state.arena.height *
            canvas.height
    };
}


function drawBackground(){

    ctx.fillStyle =
        "#030711";

    ctx.fillRect(
        0,
        0,
        canvas.width,
        canvas.height
    );

    const grid = 50;

    ctx.strokeStyle =
        "rgba(40,120,190,.12)";

    ctx.lineWidth = 1;

    for(
        let x=0;
        x<canvas.width;
        x+=grid
    ){

        ctx.beginPath();

        ctx.moveTo(
            x,
            0
        );

        ctx.lineTo(
            x,
            canvas.height
        );

        ctx.stroke();
    }

    for(
        let y=0;
        y<canvas.height;
        y+=grid
    ){

        ctx.beginPath();

        ctx.moveTo(
            0,
            y
        );

        ctx.lineTo(
            canvas.width,
            y
        );

        ctx.stroke();
    }
}


function circle(
    x,
    y,
    radius,
    fill,
    glow
){

    ctx.save();

    if(glow){

        ctx.shadowBlur = 18;
        ctx.shadowColor = fill;
    }

    ctx.fillStyle = fill;

    ctx.beginPath();

    ctx.arc(
        x,
        y,
        radius,
        0,
        Math.PI * 2
    );

    ctx.fill();

    ctx.restore();
}


function drawPlayer(player){

    const p =
        worldToScreen(
            player.x,
            player.y
        );

    const sx =
        canvas.width /
        state.arena.width;

    const radius =
        17 * sx;

    let color =
        player.slot === 1
        ? "#2aa8ff"
        : "#ff3d81";

    if(player.downed){
        color = "#ffb020";
    }

    if(player.eliminated){
        color = "#4b5666";
    }

    circle(
        p.x,
        p.y,
        radius,
        color,
        true
    );

    ctx.fillStyle = "#ffffff";

    ctx.beginPath();

    ctx.arc(
        p.x,
        p.y,
        radius * .35,
        0,
        Math.PI * 2
    );

    ctx.fill();

    // HP
    const barWidth =
        42 * sx;

    const barHeight =
        5 * sx;

    ctx.fillStyle =
        "rgba(0,0,0,.6)";

    ctx.fillRect(
        p.x - barWidth / 2,
        p.y - radius - 14,
        barWidth,
        barHeight
    );

    ctx.fillStyle =
        "#28e18d";

    ctx.fillRect(
        p.x - barWidth / 2,
        p.y - radius - 14,
        barWidth *
        (
            player.hp /
            player.max_hp
        ),
        barHeight
    );

    if(player.downed){

        ctx.fillStyle =
            "#ffb020";

        ctx.font =
            "bold 13px Arial";

        ctx.textAlign =
            "center";

        ctx.fillText(
            "DOWNED",
            p.x,
            p.y - radius - 22
        );
    }
}


function drawEnemy(enemy){

    const p =
        worldToScreen(
            enemy.x,
            enemy.y
        );

    const sx =
        canvas.width /
        state.arena.width;

    let radius =
        enemy.radius *
        sx;

    let color =
        enemy.kind === "boss"
        ? "#ff164c"
        : enemy.kind === "elite"
        ? "#ff9f1c"
        : "#a83cff";

    circle(
        p.x,
        p.y,
        radius,
        color,
        true
    );

    if(enemy.kind === "boss"){

        ctx.strokeStyle =
            "#ff6c8c";

        ctx.lineWidth = 3;

        ctx.beginPath();

        ctx.arc(
            p.x,
            p.y,
            radius + 8,
            0,
            Math.PI * 2
        );

        ctx.stroke();
    }

    const width =
        radius * 2;

    ctx.fillStyle =
        "rgba(0,0,0,.7)";

    ctx.fillRect(
        p.x - width / 2,
        p.y - radius - 10,
        width,
        4
    );

    ctx.fillStyle =
        "#ff4d6d";

    ctx.fillRect(
        p.x - width / 2,
        p.y - radius - 10,
        width *
        (
            enemy.hp /
            enemy.max_hp
        ),
        4
    );
}


function drawBullet(bullet){

    const p =
        worldToScreen(
            bullet.x,
            bullet.y
        );

    circle(
        p.x,
        p.y,
        4,
        "#e8f8ff",
        true
    );
}


function updateHUD(){

    if(!state){
        return;
    }

    const p1 =
        state.players.find(
            p => p.slot === 1
        );

    const p2 =
        state.players.find(
            p => p.slot === 2
        );

    function playerHTML(p){

        if(!p){

            return `
                <b>PLAYER 2</b>
                <div style="color:#607995">
                    Waiting...
                </div>
            `;
        }

        return `
            <b>
                ${escapeHTML(p.name)}
            </b>
            <div>
                SCORE: ${p.score}
                &nbsp; KILLS: ${p.kills}
            </div>

            <div class="bar">
                <div
                    class="hp"
                    style="
                    width:${
                        Math.max(
                            0,
                            p.hp /
                            p.max_hp *
                            100
                        )
                    }%;
                    "
                ></div>
            </div>

            <div class="bar">
                <div
                    class="xp"
                    style="
                    width:${
                        Math.min(
                            100,
                            p.xp /
                            (p.level * 250) *
                            100
                        )
                    }%;
                    "
                ></div>
            </div>

            LEVEL ${p.level}
        `;
    }

    document
        .getElementById("p1")
        .innerHTML =
        playerHTML(p1);

    document
        .getElementById("p2")
        .innerHTML =
        playerHTML(p2);

    document
        .getElementById("waveInfo")
        .textContent =
        "WAVE " +
        state.wave;
}


function escapeHTML(text){

    return String(text)
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;")
        .replaceAll("'","&#039;");
}


function updateUpgrade(){

    if(!state){
        return;
    }

    const me =
        state.players.find(
            p => p.slot === mySlot
        );

    if(
        !me ||
        !me.pending_upgrade
    ){

        upgrade.style.display =
            "none";

        return;
    }

    upgrade.style.display =
        "flex";

    optionsBox.innerHTML = "";

    me.pending_upgrade.forEach(
        option => {

            const div =
                document.createElement(
                    "div"
                );

            div.className =
                "option";

            div.innerHTML = `
                <h3>
                    ${option.title}
                </h3>

                <p>
                    ${option.description}
                </p>
            `;

            div.onclick = function(){

                ws.send(
                    JSON.stringify({
                        type:"upgrade",
                        choice:option.id
                    })
                );

                upgrade.style.display =
                    "none";
            };

            optionsBox.appendChild(
                div
            );
        }
    );
}


function showResult(){

    if(!state){
        return;
    }

    result.style.display =
        "flex";

    const won =
        state.status === "won";

    document
        .getElementById("resultTitle")
        .textContent =
        won
        ? "VICTORY"
        : "GAME OVER";

    const totalScore =
        state.players.reduce(
            (sum,p) =>
                sum + p.score,
            0
        );

    document
        .getElementById("resultScore")
        .textContent =
        "TEAM SCORE: " +
        totalScore;
}


function draw(){

    requestAnimationFrame(
        draw
    );

    if(!state){

        drawBackground();

        return;
    }

    drawBackground();

    for(
        const bullet of state.bullets
    ){
        drawBullet(bullet);
    }

    for(
        const enemy of state.enemies
    ){
        drawEnemy(enemy);
    }

    for(
        const player of state.players
    ){
        drawPlayer(player);
    }

    updateHUD();

    const message =
        document.getElementById(
            "centerMessage"
        );

    message.textContent =
        state.wave_message || "";

    if(
        state.status === "waiting"
    ){

        message.textContent =
            "WAITING FOR PLAYER 2";
    }
}


connect();
draw();

</script>

</body>
</html>
"""


@app.get("/neon", response_class=HTMLResponse)
def neon_client():
    return HTMLResponse(
        content=NEON_HTML
    )
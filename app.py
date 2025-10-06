from __future__ import annotations
import os, json
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import httpx, jwt
from passlib.hash import bcrypt
from fastapi import FastAPI, Depends, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, DateTime, UniqueConstraint, Text
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Session

# ------------------------------------------------------------------------------
# Config
# ------------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./db.sqlite3")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
ALGO = "HS256"
TOKEN_HOURS = 240

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

app = FastAPI(title="GridCap API", version="0.3")

# Restrict CORS to your Vercel domain + local dev
ALLOWED_ORIGINS = [
    os.getenv("FRONTEND_ORIGIN", "https://nfl-fpl-site.vercel.app"),
    "http://localhost:3000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*", "Authorization"],  # important
)

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# ------------------------------------------------------------------------------
# Models
# ------------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    entries = relationship("Entry", back_populates="user")

class League(Base):
    __tablename__ = "leagues"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"))
    entries = relationship("Entry", back_populates="league")

class Entry(Base):
    __tablename__ = "entries"
    id = Column(Integer, primary_key=True)
    league_id = Column(Integer, ForeignKey("leagues.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    team_name = Column(String, nullable=False)
    points = Column(Integer, default=0)
    league = relationship("League", back_populates="entries")
    user = relationship("User", back_populates="entries")
    __table_args__ = (UniqueConstraint("league_id", "user_id", name="uq_entry_league_user"),)

class Player(Base):
    __tablename__ = "players"
    id = Column(Integer, primary_key=True)
    external_id = Column(String, unique=True, index=True)
    name = Column(String, index=True, nullable=False)
    team = Column(String(4), index=True)
    pos = Column(String(4), index=True)            # QB/RB/WR/TE/K/DST
    price_m = Column(Float, default=6.0)

class Gameweek(Base):
    __tablename__ = "gameweeks"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    deadline_at = Column(DateTime, nullable=False)

class SquadPick(Base):
    __tablename__ = "squad_picks"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    gameweek = Column(Integer, index=True, nullable=False)
    player_id = Column(Integer, ForeignKey("players.id"), index=True, nullable=False)
    __table_args__ = (UniqueConstraint("user_id", "gameweek", "player_id", name="uq_squad_one"),)

class Lineup(Base):
    __tablename__ = "lineups"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    gameweek = Column(Integer, index=True, nullable=False)
    starters_json = Column(Text, nullable=False, default="[]")
    captain_id = Column(Integer, ForeignKey("players.id"), nullable=True)
    vice_captain_id = Column(Integer, ForeignKey("players.id"), nullable=True)
    chip = Column(String, nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "gameweek", name="uq_lineup_one"),)

Base.metadata.create_all(bind=engine)

# ------------------------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------------------------
def create_token(user_id: int) -> str:
    payload = {"sub": user_id, "exp": datetime.utcnow() + timedelta(hours=TOKEN_HOURS)}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGO)

def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = auth.split(" ", 1)[1]
    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=[ALGO])
        uid = int(data["sub"])
    except Exception:
        raise HTTPException(401, "Invalid token")
    user = db.query(User).filter(User.id == uid).one_or_none()
    if not user:
        raise HTTPException(401, "User not found")
    return user

# ------------------------------------------------------------------------------
# Schemas
# ------------------------------------------------------------------------------
class RegisterIn(BaseModel):
    name: str
    email: str
    password: str

class LoginIn(BaseModel):
    email: str
    password: str

class LeagueCreateIn(BaseModel):
    name: Optional[str] = "GridCap League"
    team_name: str

class LeagueJoinIn(BaseModel):
    league_id: int
    team_name: str

class SquadSetIn(BaseModel):
    gameweek: int = Field(..., ge=1)
    player_ids: List[int] = Field(..., min_items=15, max_items=15)

class LineupSetIn(BaseModel):
    gameweek: int
    starters: List[int] = Field(..., min_items=9, max_items=9)
    captain_id: int
    vice_captain_id: int
    chip: Optional[str] = None

# ------------------------------------------------------------------------------
# Auth routes
# ------------------------------------------------------------------------------
@app.post("/auth/register")
def auth_register(inp: RegisterIn, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == inp.email).first():
        raise HTTPException(400, "Email already registered")
    user = User(
        name=inp.name.strip(),
        email=inp.email.strip().lower(),
        password_hash=bcrypt.hash(inp.password),
    )
    db.add(user); db.commit(); db.refresh(user)
    token = create_token(user.id)
    return {"token": token, "user": {"id": user.id, "name": user.name, "email": user.email}}

@app.post("/auth/login")
def auth_login(inp: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == inp.email.strip().lower()).one_or_none()
    if not user or not bcrypt.verify(inp.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")
    token = create_token(user.id)
    return {"token": token, "user": {"id": user.id, "name": user.name, "email": user.email}}

@app.get("/auth/me")
def auth_me(current: User = Depends(get_current_user)):
    return {"id": current.id, "name": current.name, "email": current.email}

# ------------------------------------------------------------------------------
# League & Team
# ------------------------------------------------------------------------------
@app.post("/league/create")
def league_create(inp: LeagueCreateIn, current: User = Depends(get_current_user), db: Session = Depends(get_db)):
    league = League(name=inp.name, owner_id=current.id)
    db.add(league); db.commit(); db.refresh(league)
    entry = Entry(league_id=league.id, user_id=current.id, team_name=inp.team_name)
    db.add(entry); db.commit(); db.refresh(entry)
    return {"league_id": league.id, "entry_id": entry.id}

@app.post("/league/join")
def league_join(inp: LeagueJoinIn, current: User = Depends(get_current_user), db: Session = Depends(get_db)):
    league = db.query(League).filter(League.id == inp.league_id).one_or_none()
    if not league: raise HTTPException(404, "League not found")
    existing = db.query(Entry).filter(Entry.league_id == inp.league_id, Entry.user_id == current.id).one_or_none()
    if existing: return {"league_id": league.id, "entry_id": existing.id}
    entry = Entry(league_id=league.id, user_id=current.id, team_name=inp.team_name)
    db.add(entry); db.commit(); db.refresh(entry)
    return {"league_id": league.id, "entry_id": entry.id}

# ------------------------------------------------------------------------------
# Players
# ------------------------------------------------------------------------------
@app.get("/players")
def list_players(db: Session = Depends(get_db), current: User = Depends(get_current_user)):
    rows = db.query(Player).order_by(Player.pos, Player.price_m.desc()).all()
    return [{"id": p.id, "name": p.name, "team": p.team, "pos": p.pos, "position": p.pos, "price_m": float(p.price_m), "price": float(p.price_m)} for p in rows]

VALID_POS = {"QB", "RB", "WR", "TE", "K", "DEF"}
def price_for_player(p: Dict[str, Any]) -> float:
    base = {"QB": 8.0, "RB": 7.5, "WR": 7.5, "TE": 6.0, "K": 5.0, "DEF": 5.0}
    pos = p.get("position"); price = base.get(pos, 6.0)
    if p.get("depth_chart_order") == 1: price += 2.0
    if (p.get("years_exp") or 0) >= 5:  price += 0.7
    return round(max(4.0, min(price, 13.0)), 1)

@app.post("/players/sync")
async def sync_players(db: Session = Depends(get_db)):
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get("https://api.sleeper.app/v1/players/nfl"); r.raise_for_status()
    payload = r.json(); created = updated = 0
    for sid, p in payload.items():
        if not p or not p.get("active"): continue
        pos = p.get("position"); if pos not in VALID_POS: continue
        ext_id = str(sid)
        name = (p.get("full_name") or f"{(p.get('first_name') or '').strip()} {(p.get('last_name') or '').strip()}").strip()
        team = (p.get("team") or "").upper()
        pos_app = "DST" if pos == "DEF" else pos
        price_m = price_for_player(p)
        obj = db.query(Player).filter(Player.external_id == ext_id).one_or_none()
        if obj:
            obj.name, obj.team, obj.pos, obj.price_m = name, team, pos_app, price_m; updated += 1
        else:
            db.add(Player(external_id=ext_id, name=name, team=team, pos=pos_app, price_m=price_m)); created += 1
    db.commit()
    return {"ok": True, "created": created, "updated": updated}

# ------------------------------------------------------------------------------
# Squad / Lineup
# ------------------------------------------------------------------------------
def ensure_gw(db: Session, gw_id: int) -> Gameweek:
    gw = db.query(Gameweek).filter(Gameweek.id == gw_id).one_or_none()
    if not gw:
        gw = Gameweek(id=gw_id, name=f"GW{gw_id}", deadline_at=datetime.utcnow() + timedelta(days=7))
        db.add(gw); db.commit()
    return gw

@app.get("/squad")
def get_squad(gw: int = Query(..., ge=1), current: User = Depends(get_current_user), db: Session = Depends(get_db)):
    picks = db.query(SquadPick).filter(SquadPick.user_id == current.id, SquadPick.gameweek == gw).all()
    return {"picks": [{"player_id": sp.player_id} for sp in picks]}

@app.post("/squad/set")
def set_squad(inp: SquadSetIn, current: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ensure_gw(db, inp.gameweek)
    if len(inp.player_ids) != 15: raise HTTPException(400, "You must submit exactly 15 player_ids.")
    cnt = db.query(Player).filter(Player.id.in_(inp.player_ids)).count()
    if cnt != 15: raise HTTPException(400, "One or more player_ids not found.")
    db.query(SquadPick).filter(SquadPick.user_id == current.id, SquadPick.gameweek == inp.gameweek).delete(synchronize_session=False)
    for pid in inp.player_ids: db.add(SquadPick(user_id=current.id, gameweek=inp.gameweek, player_id=pid))
    db.commit(); return {"ok": True, "saved": 15}

@app.post("/lineup/set")
def set_lineup(inp: LineupSetIn, current: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ensure_gw(db, inp.gameweek)
    if len(inp.starters) != 9: raise HTTPException(400, "Starters must be exactly 9 players.")
    if inp.captain_id == inp.vice_captain_id: raise HTTPException(400, "Captain and vice must be different.")
    squad_ids = [sp.player_id for sp in db.query(SquadPick).filter(SquadPick.user_id == current.id, SquadPick.gameweek == inp.gameweek).all()]
    if len(squad_ids) != 15: raise HTTPException(400, "Set your 15-man squad first.")
    if not set(inp.starters).issubset(set(squad_ids)): raise HTTPException(400, "Starters must be chosen from your squad.")
    obj = db.query(Lineup).filter(Lineup.user_id == current.id, Lineup.gameweek == inp.gameweek).one_or_none()
    starters_json = json.dumps(inp.starters)
    if obj:
        obj.starters_json, obj.captain_id, obj.vice_captain_id, obj.chip = starters_json, inp.captain_id, inp.vice_captain_id, inp.chip
    else:
        db.add(Lineup(user_id=current.id, gameweek=inp.gameweek, starters_json=starters_json, captain_id=inp.captain_id, vice_captain_id=inp.vice_captain_id, chip=inp.chip))
    db.commit(); return {"ok": True}

# ------------------------------------------------------------------------------
@app.get("/standings/{league_id}")
def standings(league_id: int, current: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(Entry).filter(Entry.league_id == league_id).order_by(Entry.points.desc(), Entry.team_name.asc()).all()
    return [{"entry_id": e.id, "team_name": e.team_name, "points": e.points} for e in rows]

@app.get("/")
def root(): return {"ok": True, "service": "GridCap API"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)

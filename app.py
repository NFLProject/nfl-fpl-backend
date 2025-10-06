from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

import httpx
from fastapi import FastAPI, Depends, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, ForeignKey, DateTime, UniqueConstraint, Text
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Session

# ------------------------------------------------------------------------------
# Config
# ------------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./db.sqlite3")

# SQLite needs check_same_thread = False
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

app = FastAPI(title="GridCap API", version="0.2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # lock down in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ------------------------------------------------------------------------------
# Models
# ------------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True)

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
    external_id = Column(String, unique=True, index=True)   # Sleeper ID
    name = Column(String, index=True, nullable=False)
    team = Column(String(4), index=True)
    pos = Column(String(4), index=True)                      # QB/RB/WR/TE/K/DST
    price_m = Column(Float, default=6.0)                     # price in millions


class Gameweek(Base):
    __tablename__ = "gameweeks"
    id = Column(Integer, primary_key=True)                   # 1,2,3...
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
    starters_json = Column(Text, nullable=False, default="[]")  # JSON list of player_ids
    captain_id = Column(Integer, ForeignKey("players.id"), nullable=True)
    vice_captain_id = Column(Integer, ForeignKey("players.id"), nullable=True)
    chip = Column(String, nullable=True)  # "BB","TC","WC", or None

    __table_args__ = (UniqueConstraint("user_id", "gameweek", name="uq_lineup_one"),)


Base.metadata.create_all(bind=engine)

# ------------------------------------------------------------------------------
# Schemas
# ------------------------------------------------------------------------------

class RegisterIn(BaseModel):
    name: str
    email: str


class LeagueCreateIn(BaseModel):
    userId: Optional[int] = None
    name: Optional[str] = "UK NFL FPL League"
    team_name: Optional[str] = None
    teamName: Optional[str] = None


class LeagueJoinIn(BaseModel):
    userId: Optional[int] = None
    league_id: Optional[int] = None
    leagueId: Optional[int] = None
    team_name: Optional[str] = None
    teamName: Optional[str] = None


class SquadSetIn(BaseModel):
    gameweek: int = Field(..., ge=1)
    player_ids: List[int] = Field(..., min_items=1, max_items=30)
    # fallback keys supported by older FE:
    picks: Optional[List[int]] = None


class LineupSetIn(BaseModel):
    gameweek: int
    starters: List[int] = Field(..., min_items=1, max_items=30)
    captain_id: int
    vice_captain_id: int
    chip: Optional[str] = None


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def get_user_id(x_user: Optional[str], explicit: Optional[int]) -> int:
    """Allow user id via header X-User or JSON body."""
    if explicit:
        return int(explicit)
    if x_user:
        return int(x_user)
    raise HTTPException(status_code=400, detail="Missing user id (X-User header or body userId).")


def ensure_gameweek(db: Session, gw_id: int) -> Gameweek:
    gw = db.query(Gameweek).filter(Gameweek.id == gw_id).one_or_none()
    if not gw:
        gw = Gameweek(
            id=gw_id,
            name=f"GW{gw_id}",
            deadline_at=datetime.utcnow() + timedelta(days=7)
        )
        db.add(gw)
        db.commit()
    return gw


# ------------------------------------------------------------------------------
# Core endpoints
# ------------------------------------------------------------------------------

@app.post("/register")
def register(inp: RegisterIn, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == inp.email).one_or_none()
    if existing:
        return {"id": existing.id}
    u = User(name=inp.name, email=inp.email)
    db.add(u)
    db.commit()
    db.refresh(u)
    return {"id": u.id}


@app.get("/me")
def me(x_user: Optional[str] = Header(default=None), db: Session = Depends(get_db)):
    if not x_user:
        raise HTTPException(status_code=400, detail="X-User header required")
    u = db.query(User).filter(User.id == int(x_user)).one_or_none()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return {"id": u.id, "name": u.name, "email": u.email}


@app.post("/league/create")
def league_create(
    inp: LeagueCreateIn,
    x_user: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    user_id = get_user_id(x_user, inp.userId)
    user = db.query(User).filter(User.id == user_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    league = League(name=inp.name or "League", owner_id=user_id)
    db.add(league)
    db.commit()
    db.refresh(league)

    team_name = inp.team_name or inp.teamName or f"{user.name}'s Team"
    entry = Entry(league_id=league.id, user_id=user_id, team_name=team_name)
    db.add(entry)
    db.commit()
    db.refresh(entry)

    return {"league_id": league.id, "entry_id": entry.id}


@app.post("/league/join")
def league_join(
    inp: LeagueJoinIn,
    x_user: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    user_id = get_user_id(x_user, inp.userId)
    league_id = inp.leagueId or inp.league_id
    if not league_id:
        raise HTTPException(status_code=400, detail="league_id required")

    league = db.query(League).filter(League.id == league_id).one_or_none()
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    # upsert entry
    entry = (
        db.query(Entry)
        .filter(Entry.league_id == league_id, Entry.user_id == user_id)
        .one_or_none()
    )
    if not entry:
        team_name = inp.team_name or inp.teamName or "My Team"
        entry = Entry(league_id=league_id, user_id=user_id, team_name=team_name)
        db.add(entry)
        db.commit()
        db.refresh(entry)

    return {"league_id": league_id, "entry_id": entry.id}


@app.get("/players")
def list_players(db: Session = Depends(get_db)):
    """Return players with BOTH key styles for FE compatibility."""
    rows = db.query(Player).order_by(Player.pos, Player.price_m.desc()).all()
    out = []
    for p in rows:
        out.append({
            "id": p.id,
            "name": p.name,
            "team": p.team,
            "pos": p.pos,
            "position": p.pos,        # alias
            "price_m": float(p.price_m),
            "price": float(p.price_m) # alias
        })
    return out


# --------------------------- NEW: /players/sync (Sleeper) ---------------------

VALID_POS = {"QB", "RB", "WR", "TE", "K", "DEF"}  # Sleeper uses DEF (we map to DST)


def price_for_player(p: Dict[str, Any]) -> float:
    """
    Simple, transparent pricing heuristic:
    - base by position
    - +2.0m if depth_chart_order == 1 (projected starter)
    - +0.7m if years_exp >= 5
    bounded to [4.0, 13.0]
    """
    base = {"QB": 8.0, "RB": 7.5, "WR": 7.5, "TE": 6.0, "K": 5.0, "DEF": 5.0}
    pos = p.get("position")
    price = base.get(pos, 6.0)
    if p.get("depth_chart_order") == 1:
        price += 2.0
    if (p.get("years_exp") or 0) >= 5:
        price += 0.7
    return round(max(4.0, min(price, 13.0)), 1)


@app.post("/players/sync", tags=["players"], summary="Sync all active NFL players from Sleeper")
async def sync_players(db: Session = Depends(get_db)):
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get("https://api.sleeper.app/v1/players/nfl")
            r.raise_for_status()
            payload = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Sleeper fetch failed: {e}")

    created = 0
    updated = 0

    for sid, p in payload.items():
        if not p or not p.get("active"):
            continue
        pos = p.get("position")
        if pos not in VALID_POS:
            continue

        ext_id = str(sid)
        name = (p.get("full_name") or f"{(p.get('first_name') or '').strip()} {(p.get('last_name') or '').strip()}").strip()
        team = (p.get("team") or "").upper()
        pos_app = "DST" if pos == "DEF" else pos
        price_m = price_for_player(p)

        obj = db.query(Player).filter(Player.external_id == ext_id).one_or_none()
        if obj:
            obj.name = name
            obj.team = team
            obj.pos = pos_app
            obj.price_m = price_m
            updated += 1
        else:
            db.add(Player(
                external_id=ext_id,
                name=name,
                team=team,
                pos=pos_app,
                price_m=price_m,
            ))
            created += 1

    db.commit()
    return {"ok": True, "created": created, "updated": updated}


# ------------------------------ Squad / Lineup --------------------------------

@app.get("/squad")
def get_squad(
    user_id: int = Query(...),
    league_id: Optional[int] = Query(None),  # not used in MVP, present for FE
    gw: int = Query(..., ge=1),
    db: Session = Depends(get_db)
):
    picks = (
        db.query(SquadPick)
        .filter(SquadPick.user_id == user_id, SquadPick.gameweek == gw)
        .all()
    )
    return {"picks": [{"player_id": sp.player_id} for sp in picks]}


@app.post("/squad/set")
def set_squad(
    inp: SquadSetIn,
    x_user: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    user_id = get_user_id(x_user, None)
    ensure_gameweek(db, inp.gameweek)
    player_ids = inp.player_ids or inp.picks or []

    # enforce 15-man squad
    if len(player_ids) != 15:
        raise HTTPException(status_code=400, detail="You must submit exactly 15 player_ids.")

    # validate players exist
    count = db.query(Player).filter(Player.id.in_(player_ids)).count()
    if count != 15:
        raise HTTPException(status_code=400, detail="One or more player_ids not found.")

    # replace existing squad
    db.query(SquadPick).filter(
        SquadPick.user_id == user_id, SquadPick.gameweek == inp.gameweek
    ).delete(synchronize_session=False)

    for pid in player_ids:
        db.add(SquadPick(user_id=user_id, gameweek=inp.gameweek, player_id=pid))
    db.commit()
    return {"ok": True, "saved": 15}


@app.post("/lineup/set")
def set_lineup(
    inp: LineupSetIn,
    x_user: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    user_id = get_user_id(x_user, None)
    ensure_gameweek(db, inp.gameweek)

    # starters must be exactly 9
    if len(inp.starters) != 9:
        raise HTTPException(status_code=400, detail="Starters must be exactly 9 players.")

    # captain/vice must be among starters and different
    if inp.captain_id == inp.vice_captain_id:
        raise HTTPException(status_code=400, detail="Captain and vice must be different.")
    if inp.captain_id not in inp.starters or inp.vice_captain_id not in inp.starters:
        raise HTTPException(status_code=400, detail="Captain and vice must be among starters.")

    # starters must be subset of saved squad
    squad_ids = [
        sp.player_id
        for sp in db.query(SquadPick).filter(
            SquadPick.user_id == user_id, SquadPick.gameweek == inp.gameweek
        ).all()
    ]
    if len(squad_ids) != 15:
        raise HTTPException(status_code=400, detail="Set your 15-man squad first.")

    if not set(inp.starters).issubset(set(squad_ids)):
        raise HTTPException(status_code=400, detail="Starters must be chosen from your squad.")

    # upsert lineup
    obj = (
        db.query(Lineup)
        .filter(Lineup.user_id == user_id, Lineup.gameweek == inp.gameweek)
        .one_or_none()
    )
    starters_json = json.dumps(inp.starters)
    if obj:
        obj.starters_json = starters_json
        obj.captain_id = inp.captain_id
        obj.vice_captain_id = inp.vice_captain_id
        obj.chip = inp.chip
    else:
        obj = Lineup(
            user_id=user_id,
            gameweek=inp.gameweek,
            starters_json=starters_json,
            captain_id=inp.captain_id,
            vice_captain_id=inp.vice_captain_id,
            chip=inp.chip,
        )
        db.add(obj)
    db.commit()
    return {"ok": True}


@app.get("/standings/{league_id}")
def standings(league_id: int, db: Session = Depends(get_db)):
    """Very simple standings: returns team name and points (default 0 in MVP)."""
    rows = (
        db.query(Entry)
        .filter(Entry.league_id == league_id)
        .order_by(Entry.points.desc(), Entry.team_name.asc())
        .all()
    )
    return [{"entry_id": e.id, "team_name": e.team_name, "points": e.points} for e in rows]


# ------------------------------ Utilities / Admin -----------------------------

class GWCreateIn(BaseModel):
    id: int
    name: Optional[str] = None
    deadline_at: Optional[datetime] = None


@app.post("/gameweeks/create")
def create_gw(inp: GWCreateIn, db: Session = Depends(get_db)):
    gw = db.query(Gameweek).filter(Gameweek.id == inp.id).one_or_none()
    if gw:
        raise HTTPException(status_code=400, detail="Gameweek already exists")
    gw = Gameweek(
        id=inp.id,
        name=inp.name or f"GW{inp.id}",
        deadline_at=inp.deadline_at or (datetime.utcnow() + timedelta(days=7)),
    )
    db.add(gw)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------------------
# Root
# ------------------------------------------------------------------------------

@app.get("/")
def root():
    return {"ok": True, "service": "GridCap API"}


# ------------------------------------------------------------------------------
# Run (local dev)
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)

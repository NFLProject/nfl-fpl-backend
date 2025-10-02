from __future__ import annotations
import enum, secrets
from datetime import datetime, timedelta
from typing import List, Optional, Dict
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, Boolean, DateTime, Enum as SAEnum, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

# --- DB setup (SQLite file) ---
DATABASE_URL = "sqlite:///./fantasy_nfl_fpl.sqlite3"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- App + CORS ---
app = FastAPI(title="NFL Fantasy (FPL-style)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # for MVP; later restrict to your Vercel frontend origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Models ---
class Position(str, enum.Enum): QB="QB"; RB="RB"; WR="WR"; TE="TE"; K="K"; DST="DST"

INITIAL_BUDGET = 100.0

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, index=True)
    name = Column(String)
    token = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class League(Base):
    __tablename__ = "leagues"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    creator_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

class LeagueEntry(Base):
    __tablename__ = "league_entries"
    id = Column(Integer, primary_key=True)
    league_id = Column(Integer, ForeignKey("leagues.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    team_name = Column(String)
    budget = Column(Float, default=INITIAL_BUDGET)
    total_points = Column(Integer, default=0)
    __table_args__ = (UniqueConstraint("league_id","user_id",name="uq_league_user"),)

class Player(Base):
    __tablename__ = "players"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    team = Column(String)
    position = Column(SAEnum(Position))

class PlayerPrice(Base):
    __tablename__ = "player_prices"
    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("players.id"))
    price = Column(Float)
    effective_from_gw = Column(Integer, default=1)

class Gameweek(Base):
    __tablename__ = "gameweeks"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    deadline_at = Column(DateTime)
    finished = Column(Boolean, default=False)

class Squad(Base):
    __tablename__ = "squads"
    id = Column(Integer, primary_key=True)
    entry_id = Column(Integer, ForeignKey("league_entries.id"))
    gameweek_id = Column(Integer, ForeignKey("gameweeks.id"))
    player_ids_csv = Column(String)      # 15 ids
    starters_csv = Column(String)        # 9 ids
    captain_id = Column(Integer, nullable=True)
    vice_captain_id = Column(Integer, nullable=True)
    chips = Column(String, default="")
    __table_args__ = (UniqueConstraint("entry_id","gameweek_id", name="uq_entry_gw"),)

Base.metadata.create_all(bind=engine)

# --- Helpers ---
def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def current_user(x_user: Optional[str] = Header(None), db: Session = Depends(get_db)) -> User:
    if not x_user: raise HTTPException(401, "Missing X-User header")
    u = db.query(User).filter(User.id==int(x_user)).first()
    if not u: raise HTTPException(401, "Invalid X-User")
    return u

def parse_ids(csv: str) -> List[int]:
    return [int(x) for x in csv.split(",") if x.strip()] if csv else []

# --- Schemas ---
class RegisterReq(BaseModel): email:str; name:str
class RegisterRes(BaseModel): id:int; token:str
class LeagueCreateReq(BaseModel): name:str; team_name:str
class JoinLeagueReq(BaseModel): league_id:int; team_name:str
class PlayerSeedItem(BaseModel): name:str; team:str; position:Position; price:float
class SeedReq(BaseModel): players:List[PlayerSeedItem]
class GwCreateReq(BaseModel): id:int; name:str; deadline_at:datetime
class SquadSetReq(BaseModel): gameweek:int=Field(...,ge=1); player_ids:List[int]
class LineupSetReq(BaseModel): gameweek:int; starters:List[int]; captain_id:int; vice_captain_id:int; chip:Optional[str]=None

# --- Routes ---
@app.get("/health")
def health(): return {"ok": True, "time": datetime.utcnow().isoformat()}

@app.post("/register", response_model=RegisterRes)
def register(req: RegisterReq, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email==req.email).first():
        raise HTTPException(400, "Email already registered")
    token = secrets.token_hex(16)
    u = User(email=req.email, name=req.name, token=token)
    db.add(u); db.commit(); db.refresh(u)
    return RegisterRes(id=u.id, token=u.token)

@app.get("/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "email": user.email, "name": user.name}

@app.post("/league/create")
def create_league(req: LeagueCreateReq, user: User = Depends(current_user), db: Session = Depends(get_db)):
    lg = League(name=req.name, creator_id=user.id); db.add(lg); db.commit(); db.refresh(lg)
    le = LeagueEntry(league_id=lg.id, user_id=user.id, team_name=req.team_name); db.add(le); db.commit(); db.refresh(le)
    return {"league_id": lg.id, "entry_id": le.id}

@app.post("/league/join")
def join_league(req: JoinLeagueReq, user: User = Depends(current_user), db: Session = Depends(get_db)):
    lg = db.query(League).get(req.league_id)
    if not lg: raise HTTPException(404, "League not found")
    if db.query(LeagueEntry).filter_by(league_id=lg.id, user_id=user.id).first():
        raise HTTPException(400, "Already joined")
    le = LeagueEntry(league_id=lg.id, user_id=user.id, team_name=req.team_name); db.add(le); db.commit(); db.refresh(le)
    return {"entry_id": le.id}

@app.post("/players/seed")
def seed_players(req: SeedReq, user: User = Depends(current_user), db: Session = Depends(get_db)):
    for it in req.players:
        p = db.query(Player).filter_by(name=it.name, team=it.team, position=it.position).first()
        if not p:
            p = Player(name=it.name, team=it.team, position=it.position)
            db.add(p); db.commit(); db.refresh(p)
        if not db.query(PlayerPrice).filter_by(player_id=p.id, effective_from_gw=1).first():
            db.add(PlayerPrice(player_id=p.id, price=it.price, effective_from_gw=1)); db.commit()
    return {"status":"ok"}

@app.get("/players")
def list_players(db: Session = Depends(get_db)):
    items = db.query(Player).all()
    out=[]
    for p in items:
        pr = db.query(PlayerPrice).filter_by(player_id=p.id).order_by(PlayerPrice.effective_from_gw.desc()).first()
        out.append({"id":p.id,"name":p.name,"team":p.team,"position":p.position.value,"price":pr.price if pr else None})
    return out

@app.post("/gameweeks/create")
def create_gw(req: GwCreateReq, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if db.query(Gameweek).get(req.id): raise HTTPException(400,"GW exists")
    db.add(Gameweek(id=req.id, name=req.name, deadline_at=req.deadline_at)); db.commit()
    return {"status":"ok"}

@app.post("/squad/set")
def set_squad(req: SquadSetReq, user: User = Depends(current_user), db: Session = Depends(get_db)):
    le = db.query(LeagueEntry).filter_by(user_id=user.id).order_by(LeagueEntry.id.desc()).first()
    if not le: raise HTTPException(400,"Join a league first")
    # (MVP) skip deep validation
    csv = ",".join(map(str, req.player_ids))
    sq = db.query(Squad).filter_by(entry_id=le.id, gameweek_id=req.gameweek).first()
    if not sq:
        sq = Squad(entry_id=le.id, gameweek_id=req.gameweek, player_ids_csv=csv)
        db.add(sq)
    else:
        sq.player_ids_csv = csv
    db.commit()
    return {"status":"ok"}

@app.post("/lineup/set")
def set_lineup(req: LineupSetReq, user: User = Depends(current_user), db: Session = Depends(get_db)):
    le = db.query(LeagueEntry).filter_by(user_id=user.id).order_by(LeagueEntry.id.desc()).first()
    if not le: raise HTTPException(400,"Join a league first")
    sq = db.query(Squad).filter_by(entry_id=le.id, gameweek_id=req.gameweek).first()
    if not sq: raise HTTPException(400,"Set a squad first")
    sq.starters_csv = ",".join(map(str, req.starters))
    sq.captain_id = req.captain_id
    sq.vice_captain_id = req.vice_captain_id
    sq.chips = req.chip or ""
    db.commit()
    return {"status":"ok"}

@app.get("/standings/{league_id}")
def standings(league_id:int, db: Session = Depends(get_db)):
    rows = db.query(LeagueEntry).filter_by(league_id=league_id).all()
    return [{"entry_id":r.id, "team_name":r.team_name, "points":r.total_points} for r in rows]

# --- Demo seed (quick start) ---
@app.post("/demo/seed_all")
def demo_seed(db: Session = Depends(get_db)):
    # seed a few players with prices
    demo = [
        ("Patrick Mahomes","KC",Position.QB, 12.5),
        ("Christian McCaffrey","SF",Position.RB, 10.5),
        ("Justin Jefferson","MIN",Position.WR, 11.5),
        ("Travis Kelce","KC",Position.TE, 10.0),
        ("Justin Tucker","BAL",Position.K, 5.5),
        ("49ers DST","SF",Position.DST, 5.0),
    ]
    for n,t,pos,price in demo:
        p = db.query(Player).filter_by(name=n, team=t, position=pos).first()
        if not p:
            p = Player(name=n, team=t, position=pos); db.add(p); db.commit(); db.refresh(p)
        if not db.query(PlayerPrice).filter_by(player_id=p.id, effective_from_gw=1).first():
            db.add(PlayerPrice(player_id=p.id, price=price, effective_from_gw=1)); db.commit()
    # seed a GW
    if not db.query(Gameweek).get(1):
        db.add(Gameweek(id=1, name="GW1", deadline_at=datetime.utcnow()+timedelta(days=1))); db.commit()
    return {"status":"ok"}

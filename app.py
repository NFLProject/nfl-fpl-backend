"""
NFL Fantasy League (FPL-style) — FastAPI backend

Features:
- Salary-cap game: 15-player squads, 9 starters (with FLEX), captain/vice, chips.
- Managers can all own the same players (like FPL).
- Weekly transfers: 1 free, extras = -4 points.
- Upload player stats per gameweek, compute points, update standings.
- Simple header-based auth using X-User.
"""

from __future__ import annotations
import enum, secrets
from datetime import datetime, timedelta
from typing import List, Optional, Dict
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, ForeignKey, Boolean, DateTime, Enum, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

# -------------------------
# Setup
# -------------------------
DATABASE_URL = "sqlite:///./fantasy_nfl_fpl.sqlite3"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

app = FastAPI(title="NFL Fantasy (FPL-style)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # in production restrict to your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# Config
# -------------------------
class Position(str, enum.Enum):
    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    K = "K"
    DST = "DST"

STARTING_FORMATION = {Position.QB:1, Position.RB:2, Position.WR:2, Position.TE:1, Position.K:1, Position.DST:1}
SQUAD_LIMITS = {Position.QB:2, Position.RB:5, Position.WR:5, Position.TE:2, Position.K:1, Position.DST:1}
SQUAD_SIZE_TARGET = 15
INITIAL_BUDGET = 100.0
FREE_TRANSFERS_PER_GW = 1
HIT_COST = 4

# -------------------------
# Models
# -------------------------
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

class Player(Base):
    __tablename__ = "players"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    team = Column(String)
    position = Column(Enum(Position))

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

class PlayerStat(Base):
    __tablename__ = "player_stats"
    id = Column(Integer, primary_key=True)
    gameweek_id = Column(Integer, ForeignKey("gameweeks.id"))
    player_id = Column(Integer, ForeignKey("players.id"))

    # Offense
    pass_yd = Column(Integer, default=0)
    pass_td = Column(Integer, default=0)
    int_thrown = Column(Integer, default=0)
    rush_yd = Column(Integer, default=0)
    rush_td = Column(Integer, default=0)
    rec = Column(Integer, default=0)
    rec_yd = Column(Integer, default=0)
    rec_td = Column(Integer, default=0)
    fumble_lost = Column(Integer, default=0)

    # Kicking
    fg_made = Column(Integer, default=0)
    fg_miss = Column(Integer, default=0)
    xp_made = Column(Integer, default=0)
    xp_miss = Column(Integer, default=0)

    # Defense/Special Teams
    dst_sacks = Column(Integer, default=0)
    dst_int = Column(Integer, default=0)
    dst_fumrec = Column(Integer, default=0)
    dst_td = Column(Integer, default=0)
    points_allowed = Column(Integer, default=0)


"""
SQLAlchemy ORM models — mirror the TimescaleDB schema in init.sql.
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from db.connection import Base


class MarketCandle(Base):
    __tablename__ = "market_candles"
    __table_args__ = (UniqueConstraint("time", "symbol", name="market_candles_time_symbol_idx"),)

    time:   Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), primary_key=True)
    symbol: Mapped[str]      = mapped_column(String(64), primary_key=True)
    open:   Mapped[float]    = mapped_column(Numeric(12, 2))
    high:   Mapped[float]    = mapped_column(Numeric(12, 2))
    low:    Mapped[float]    = mapped_column(Numeric(12, 2))
    close:  Mapped[float]    = mapped_column(Numeric(12, 2))
    volume: Mapped[int]      = mapped_column(BigInteger, default=0)
    vwap:   Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    rsi:    Mapped[Optional[float]] = mapped_column(Numeric(6, 3), nullable=True)
    ema_9:  Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    ema_21: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)


class DailyIndicator(Base):
    __tablename__ = "daily_indicators"

    date:          Mapped[date]  = mapped_column(Date, primary_key=True)
    symbol:        Mapped[str]   = mapped_column(String(64), primary_key=True)
    prev_high:     Mapped[float] = mapped_column(Numeric(12, 2))
    prev_low:      Mapped[float] = mapped_column(Numeric(12, 2))
    prev_close:    Mapped[float] = mapped_column(Numeric(12, 2))
    pivot:         Mapped[float] = mapped_column(Numeric(12, 2))
    bc:            Mapped[float] = mapped_column(Numeric(12, 2))
    tc:            Mapped[float] = mapped_column(Numeric(12, 2))
    r1:            Mapped[float] = mapped_column(Numeric(12, 2))
    r2:            Mapped[float] = mapped_column(Numeric(12, 2))
    r3:            Mapped[float] = mapped_column(Numeric(12, 2))
    s1:            Mapped[float] = mapped_column(Numeric(12, 2))
    s2:            Mapped[float] = mapped_column(Numeric(12, 2))
    s3:            Mapped[float] = mapped_column(Numeric(12, 2))
    cpr_width_pct: Mapped[float] = mapped_column(Numeric(6, 4))


class AiDecision(Base):
    __tablename__ = "ai_decisions"

    decision_id:         Mapped[str]            = mapped_column(String(64), primary_key=True)
    time:                Mapped[datetime]        = mapped_column(TIMESTAMP(timezone=True), index=True)
    symbol:              Mapped[str]             = mapped_column(String(64), index=True)
    decision:            Mapped[str]             = mapped_column(String(8))
    confidence:          Mapped[float]           = mapped_column(Numeric(4, 3))
    reasoning:           Mapped[str]             = mapped_column(Text)
    stop_loss:           Mapped[float]           = mapped_column(Numeric(12, 2), default=0)
    target:              Mapped[float]           = mapped_column(Numeric(12, 2), default=0)
    risk_reward:         Mapped[float]           = mapped_column(Numeric(6, 2), default=0)
    indicators_snapshot: Mapped[Optional[dict]]  = mapped_column(JSONB, nullable=True)
    acted_upon:          Mapped[bool]            = mapped_column(Boolean, default=False)
    trade_id:            Mapped[Optional[str]]   = mapped_column(String(64), nullable=True)
    historical_context:  Mapped[Optional[dict]]  = mapped_column(JSONB, nullable=True)


class Trade(Base):
    __tablename__ = "trades"

    trade_id:    Mapped[str]            = mapped_column(String(64), primary_key=True)
    symbol:      Mapped[str]            = mapped_column(String(64), index=True)
    side:        Mapped[str]            = mapped_column(String(8))
    quantity:    Mapped[int]            = mapped_column(Integer)
    entry_price: Mapped[float]          = mapped_column(Numeric(12, 2))
    entry_time:  Mapped[datetime]       = mapped_column(TIMESTAMP(timezone=True), index=True)
    exit_price:  Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    exit_time:   Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    pnl:         Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    pnl_pct:     Mapped[Optional[float]] = mapped_column(Numeric(8, 4), nullable=True)
    commission:  Mapped[float]          = mapped_column(Numeric(10, 2), default=0)
    slippage:    Mapped[float]          = mapped_column(Numeric(10, 2), default=0)
    status:      Mapped[str]            = mapped_column(String(16), default="OPEN")
    decision_id: Mapped[Optional[str]]  = mapped_column(String(64), nullable=True)
    reasoning:   Mapped[Optional[str]]  = mapped_column(Text, nullable=True)


class NewsItem(Base):
    __tablename__ = "news_items"

    id:              Mapped[int]   = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    time:            Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), index=True)
    title:           Mapped[str]  = mapped_column(Text)
    summary:         Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source:          Mapped[str]  = mapped_column(String(128))
    sentiment_score: Mapped[float] = mapped_column(Numeric(4, 3), default=0.0)

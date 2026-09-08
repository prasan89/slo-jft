import json
import os
from sqlalchemy import create_engine, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.engine import URL

class Base(DeclarativeBase): pass

class Signal(Base):
    __tablename__ = 'signals'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(40), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    entry: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    r1: Mapped[float|None] = mapped_column(Float)
    r2: Mapped[float|None] = mapped_column(Float)
    r3: Mapped[float|None] = mapped_column(Float)
    s1: Mapped[float|None] = mapped_column(Float)
    s2: Mapped[float|None] = mapped_column(Float)
    s3: Mapped[float|None] = mapped_column(Float)
    ltp: Mapped[float|None] = mapped_column(Float)
    pnl: Mapped[float] = mapped_column(Float, default=0)
    pnl_pct: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(12), default='OPEN')
    timeframe: Mapped[str|None] = mapped_column(String(20))
    signal_time: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[str] = mapped_column(String(50), nullable=False)

class JFTLevel(Base):
    __tablename__ = 'jft_levels'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[str] = mapped_column(String(12), nullable=False)
    symbol: Mapped[str] = mapped_column(String(40), nullable=False)
    prev_high: Mapped[float] = mapped_column(Float, nullable=False)
    prev_low: Mapped[float] = mapped_column(Float, nullable=False)
    midpoint: Mapped[float] = mapped_column(Float, nullable=False)
    r1: Mapped[float] = mapped_column(Float, nullable=False)
    r2: Mapped[float] = mapped_column(Float, nullable=False)
    r3: Mapped[float] = mapped_column(Float, nullable=False)
    s1: Mapped[float] = mapped_column(Float, nullable=False)
    s2: Mapped[float] = mapped_column(Float, nullable=False)
    s3: Mapped[float] = mapped_column(Float, nullable=False)
    __table_args__ = (UniqueConstraint('trade_date','symbol', name='uq_jft_date_symbol'),)

class ScannerState(Base):
    __tablename__ = 'scanner_state'
    trade_date: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(40), primary_key=True)
    ltp: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(50), nullable=False)

def database_url():
    explicit = os.getenv('DATABASE_URL')
    if explicit: return explicit
    vcap = os.getenv('VCAP_SERVICES')
    if vcap:
        data = json.loads(vcap)
        for services in data.values():
            for service in services:
                c = service.get('credentials', {})
                if c.get('host') and c.get('user') and c.get('password'):
                    return URL.create('hana+hdbcli', username=c['user'], password=c['password'], host=c['host'], port=int(c.get('port',443)), query={'encrypt':'true','sslValidateCertificate':'false'})
    return f"sqlite:///{os.getenv('DB_PATH','signals.db')}"

ENGINE = create_engine(database_url(), pool_pre_ping=True, pool_recycle=300)
Base.metadata.create_all(ENGINE)

def session(): return Session(ENGINE)

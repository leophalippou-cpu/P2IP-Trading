from sqlalchemy import Column, Integer, Float, String
from .db import Base

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, unique=True, index=True)
    qty = Column(Float, default=0.0)
    avg = Column(Float, default=0.0)

class Wallet(Base):
    __tablename__ = "wallet"
    id = Column(Integer, primary_key=True)
    cash = Column(Float, default=10000.0)

class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True, index=True)
    t = Column(Integer, index=True)
    type = Column(String)   # BUY / SELL
    symbol = Column(String)
    qty = Column(Float)
    price = Column(Float)
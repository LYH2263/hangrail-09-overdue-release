from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.router import api_router
from app.database import Base, get_db
from app.models.models import HangRail, RailPlacement, Store, WorkOrder


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False)
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def client(db_session):
    app = FastAPI()
    app.include_router(api_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _make_store_and_rail(db, length_cm=100.0):
    store = Store(name="测试店")
    db.add(store)
    db.flush()
    rail = HangRail(store_id=store.id, label="A 杆", length_cm=length_cm)
    db.add(rail)
    db.flush()
    return store, rail


def _order(db, store, ticket, length_cm, status, due_at, hung_at=None):
    o = WorkOrder(
        store_id=store.id,
        ticket_code=ticket,
        garment_name="测试衣",
        length_cm=length_cm,
        status=status,
        due_at=due_at,
        hung_at=hung_at,
    )
    db.add(o)
    db.flush()
    return o


def test_hung_overdue_releases_active_placement(client, db_session):
    now = datetime.utcnow()
    store, rail = _make_store_and_rail(db_session)
    hung = _order(
        db_session, store, "HR-1", 60, "hung",
        due_at=now - timedelta(hours=1), hung_at=now - timedelta(days=1),
    )
    db_session.add(
        RailPlacement(rail_id=rail.id, order_id=hung.id, start_cm=0, end_cm=60, active=1)
    )
    db_session.commit()

    resp = client.post("/api/overdue/scan")
    assert resp.status_code == 200
    assert [o["ticket_code"] for o in resp.json()] == ["HR-1"]

    db_session.expire_all()
    assert db_session.get(WorkOrder, hung.id).status == "overdue"
    placement = db_session.scalar(
        select(RailPlacement).where(RailPlacement.order_id == hung.id)
    )
    assert placement is not None
    assert placement.active == 0

    # 票号从占位图消失
    occ = client.get(f"/api/occupancy/{rail.id}").json()
    assert [s["ticket_code"] for s in occ["segments"]] == []

    # 释放出的 60cm 空隙可再挂新衣（整杆 100cm 现已可用）
    fresh = _order(db_session, store, "HR-2", 80, "ready", due_at=now + timedelta(days=1))
    db_session.commit()
    resp = client.post("/api/hang", json={"order_id": fresh.id, "rail_id": rail.id})
    assert resp.status_code == 200
    assert resp.json()["status"] == "hung"
    occ = client.get(f"/api/occupancy/{rail.id}").json()
    assert [s["ticket_code"] for s in occ["segments"]] == ["HR-2"]


def test_ready_overdue_does_not_create_ghost_placement(client, db_session):
    now = datetime.utcnow()
    store, rail = _make_store_and_rail(db_session)
    ready = _order(db_session, store, "HR-3", 30, "ready", due_at=now - timedelta(hours=1))
    db_session.commit()

    resp = client.post("/api/overdue/scan")
    assert resp.status_code == 200
    assert [o["ticket_code"] for o in resp.json()] == ["HR-3"]

    db_session.expire_all()
    assert db_session.get(WorkOrder, ready.id).status == "overdue"
    # 未上杆过的工单不得产生任何占位行
    ghosts = db_session.scalars(
        select(RailPlacement).where(RailPlacement.order_id == ready.id)
    ).all()
    assert ghosts == []
    occ = client.get(f"/api/occupancy/{rail.id}").json()
    assert occ["segments"] == []


def test_hung_not_due_is_not_released(client, db_session):
    now = datetime.utcnow()
    store, rail = _make_store_and_rail(db_session)
    hung = _order(
        db_session, store, "HR-4", 60, "hung",
        due_at=now + timedelta(days=1), hung_at=now - timedelta(hours=2),
    )
    db_session.add(
        RailPlacement(rail_id=rail.id, order_id=hung.id, start_cm=0, end_cm=60, active=1)
    )
    db_session.commit()

    resp = client.post("/api/overdue/scan")
    assert resp.status_code == 200
    assert resp.json() == []

    db_session.expire_all()
    assert db_session.get(WorkOrder, hung.id).status == "hung"
    placement = db_session.scalar(
        select(RailPlacement).where(RailPlacement.order_id == hung.id)
    )
    assert placement.active == 1
    occ = client.get(f"/api/occupancy/{rail.id}").json()
    assert [s["ticket_code"] for s in occ["segments"]] == ["HR-4"]

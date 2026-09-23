from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.models import HangRail, RailPlacement, Store, WorkOrder

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)


def _override_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_db
client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_tables():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _setup_world():
    db = TestingSessionLocal()
    store = Store(name="测试店")
    db.add(store)
    db.flush()
    rail = HangRail(store_id=store.id, label="A 杆", length_cm=40)
    db.add(rail)
    db.flush()
    return db, store, rail


def _make_order(db, store, ticket, length, status, due_in_hours, hung=False):
    now = datetime.utcnow()
    order = WorkOrder(
        store_id=store.id,
        ticket_code=ticket,
        garment_name="测试衣",
        length_cm=length,
        status=status,
        due_at=now + timedelta(hours=due_in_hours),
        hung_at=now - timedelta(hours=2) if hung else None,
    )
    db.add(order)
    db.flush()
    return order


def _placement(db, rail, order, start, end, active=1):
    p = RailPlacement(rail_id=rail.id, order_id=order.id, start_cm=start, end_cm=end, active=active)
    db.add(p)
    db.flush()
    return p


def test_hung_overdue_releases_active_placement():
    db, store, rail = _setup_world()
    order = _make_order(db, store, "HR-OV-1", 40, "hung", due_in_hours=-1, hung=True)
    _placement(db, rail, order, 0, 40)
    db.commit()

    res = client.post("/api/overdue/scan")
    assert res.status_code == 200
    marked = {o["ticket_code"]: o for o in res.json()}
    assert "HR-OV-1" in marked
    assert marked["HR-OV-1"]["status"] == "overdue"

    db.expire_all()
    p = db.scalar(select(RailPlacement).where(RailPlacement.order_id == order.id))
    assert p is not None
    assert p.active == 0  # hung 逾期后 active=0

    # 票号从占位图消失
    occ = client.get(f"/api/occupancy/{rail.id}").json()
    assert occ["segments"] == []

    # 杆上厘米可再挂新衣：释放前 40cm 满杆会 409，释放后应成功
    new_order = _make_order(db, store, "HR-OV-1-NEW", 40, "ready", due_in_hours=24)
    db.commit()
    hang = client.post("/api/hang", json={"order_id": new_order.id, "rail_id": rail.id})
    assert hang.status_code == 200
    assert hang.json()["status"] == "hung"
    occ = client.get(f"/api/occupancy/{rail.id}").json()
    assert [s["ticket_code"] for s in occ["segments"]] == ["HR-OV-1-NEW"]


def test_ready_overdue_creates_no_ghost_placement():
    db, store, rail = _setup_world()
    order = _make_order(db, store, "HR-OV-2", 30, "ready", due_in_hours=-1)
    db.commit()

    res = client.post("/api/overdue/scan")
    assert res.status_code == 200
    marked = {o["ticket_code"]: o for o in res.json()}
    assert marked["HR-OV-2"]["status"] == "overdue"

    db.expire_all()
    # ready 逾期不产生幽灵占位
    ghosts = db.scalars(select(RailPlacement).where(RailPlacement.order_id == order.id)).all()
    assert ghosts == []
    occ = client.get(f"/api/occupancy/{rail.id}").json()
    assert occ["segments"] == []


def test_hung_not_due_is_not_released():
    db, store, rail = _setup_world()
    order = _make_order(db, store, "HR-OV-3", 40, "hung", due_in_hours=24, hung=True)
    _placement(db, rail, order, 0, 40)
    db.commit()

    res = client.post("/api/overdue/scan")
    assert res.status_code == 200
    assert all(o["ticket_code"] != "HR-OV-3" for o in res.json())

    db.expire_all()
    db_order = db.get(WorkOrder, order.id)
    assert db_order.status == "hung"
    p = db.scalar(select(RailPlacement).where(RailPlacement.order_id == order.id))
    assert p.active == 1  # 未到期 hung 衣不得被误释放

    occ = client.get(f"/api/occupancy/{rail.id}").json()
    assert [s["ticket_code"] for s in occ["segments"]] == ["HR-OV-3"]

    # 仍占着满杆，新衣挂不上
    new_order = _make_order(db, store, "HR-OV-3-NEW", 40, "ready", due_in_hours=24)
    db.commit()
    hang = client.post("/api/hang", json={"order_id": new_order.id, "rail_id": rail.id})
    assert hang.status_code == 409

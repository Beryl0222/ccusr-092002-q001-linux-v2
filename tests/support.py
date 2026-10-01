"""测试共用的场景构造。"""

from datetime import datetime, timedelta, timezone

from coldchain import (
    AppendOnlyStore,
    Binding,
    CalibrationCertificate,
    ClockDrift,
    Device,
    Lot,
    bind_device,
    ingest_reading,
    register_calibration,
    register_device,
    register_drift,
    register_lot,
)

TZ = timezone(timedelta(hours=8))
LOT_ID = "YX-20260914-ROSE-01"
SEGMENT = "SEG-TRUNK-KMG"
CARRIER = "carrier-luyun-88"
WINDOW_START = "2026-09-14T06:00:00+08:00"
WINDOW_END = "2026-09-14T12:00:00+08:00"


def ts(hour: int, minute: int = 0) -> str:
    return datetime(2026, 9, 14, hour, minute, tzinfo=TZ).isoformat()


def build_store(*, with_devices=("D1",), cert_valid_until="2027-01-01T00:00:00+08:00"):
    """构造一个批次、一个区段和若干设备的基础场景。"""
    store = AppendOnlyStore()
    register_lot(
        store,
        Lot(
            lot_id=LOT_ID,
            cultivar="红色切花月季",
            harvested_at="2026-09-14T05:40:00+08:00",
            route_id="KMG-BAV-D1",
            packaging="保湿棉+打孔膜",
            temp_min_c=2.0,
            temp_max_c=6.0,
        ),
    )
    for device_id in with_devices:
        register_device(
            store,
            Device(
                device_id=device_id,
                model="M1",
                owner_id="coop-yuxi-01",
                owner_name="玉溪某合作社",
                serial=f"SN-{device_id}-0001",
                install_position="车厢前部",
                tolerance_c=0.5,
            ),
        )
        register_calibration(
            store,
            CalibrationCertificate(
                cert_id=f"CERT-{device_id}-1",
                device_id=device_id,
                lab="省计量院",
                issued_at="2026-01-01T00:00:00+08:00",
                valid_from="2026-01-01T00:00:00+08:00",
                valid_until=cert_valid_until,
                uncertainty_c=0.2,
            ),
        )
        bind_device(
            store,
            Binding(
                device_id=device_id,
                lot_id=LOT_ID,
                segment_id=SEGMENT,
                carrier_id=CARRIER,
                started_at=WINDOW_START,
                stopped_at=WINDOW_END,
            ),
        )
    return store


def fill_readings(store, device_id="D1", temp=4.0, start_hour=6, end_hour=12, step_minutes=5):
    """按固定间隔补一批读数，接收时间即设备时间（非补传）。"""
    t = datetime(2026, 9, 14, start_hour, tzinfo=TZ)
    end = datetime(2026, 9, 14, end_hour, tzinfo=TZ)
    while t <= end:
        ingest_reading(
            store,
            device_id=device_id,
            lot_id=LOT_ID,
            segment_id=SEGMENT,
            device_time=t.isoformat(),
            temperature_c=temp,
            received_time=t.isoformat(),
        )
        t += timedelta(minutes=step_minutes)

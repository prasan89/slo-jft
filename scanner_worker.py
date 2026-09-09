import time
import traceback
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import scanner

IST = ZoneInfo("Asia/Kolkata")
INTERVAL = int(scanner.os.getenv("SCANNER_INTERVAL_SECONDS", "300"))


def now_ist():
    return datetime.now(IST)


def in_market(now):
    return now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 30)


def seconds_until_next_slot(now):
    """Align scans to 5-minute clock boundaries instead of process start time."""
    minute = now.minute
    next_minute = ((minute // INTERVAL_MINUTES) + 1) * INTERVAL_MINUTES
    if next_minute >= 60:
        next_slot = now.replace(hour=now.hour + 1, minute=0, second=0, microsecond=0)
    else:
        next_slot = now.replace(minute=next_minute, second=0, microsecond=0)
    return max(1, int((next_slot - now).total_seconds()))


INTERVAL_MINUTES = max(1, INTERVAL // 60)


def main():
    print(f"JFT worker started; interval={INTERVAL}s ({INTERVAL_MINUTES}m), timezone=Asia/Kolkata")
    while True:
        now = now_ist()
        try:
            if in_market(now):
                scanner.run()
            else:
                print(f"Outside NSE session: {now.isoformat()}")
        except Exception:
            traceback.print_exc()

        # Keep the worker alive, but align the next run to :00/:05/:10/... IST.
        # This gives the dashboard a predictable 5-minute scanner cadence.
        delay = seconds_until_next_slot(now_ist())
        time.sleep(delay)


if __name__ == "__main__":
    main()

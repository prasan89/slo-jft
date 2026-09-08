import os
import time
import traceback
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import scanner

IST = ZoneInfo('Asia/Kolkata')
INTERVAL = int(os.getenv('SCANNER_INTERVAL_SECONDS','300'))


def in_market():
    now=datetime.now(IST)
    return now.weekday()<5 and dtime(9,15)<=now.time()<=dtime(15,30)


def main():
    print(f'JFT worker started; interval={INTERVAL}s')
    while True:
        try:
            if in_market(): scanner.run()
            else: print(f'Outside NSE session: {datetime.now(IST).isoformat()}')
        except Exception:
            traceback.print_exc()
        time.sleep(max(60,INTERVAL))

if __name__=='__main__': main()

# run_scheduler.py
# Runs the reporting agent on a cron schedule defined in config.py.
#
# Usage:
#   python3.12 run_scheduler.py
#
# To override schedule or period without editing config.py:
#   REPORT_CRON="0 8 * * *" REPORT_PERIOD_DAYS=14 python3.12 run_scheduler.py
#
# Runs until interrupted (Ctrl-C). Suitable for running as a background process
# or system service (launchd / systemd).
import os
import signal
import sys
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config import REPORT_CRON, REPORT_PERIOD_DAYS
from agent.reporter import generate_and_save_report


def _report_job() -> None:
    period = int(os.environ.get("REPORT_PERIOD_DAYS", REPORT_PERIOD_DAYS))
    generate_and_save_report(period_days=period)


def main() -> None:
    cron = os.environ.get("REPORT_CRON", REPORT_CRON)
    period = int(os.environ.get("REPORT_PERIOD_DAYS", REPORT_PERIOD_DAYS))

    scheduler = BlockingScheduler()
    trigger = CronTrigger.from_crontab(cron)
    scheduler.add_job(_report_job, trigger)

    # A job's next_run_time is only populated once the scheduler is running, so
    # ask the trigger directly — this works before start().
    next_run = trigger.get_next_fire_time(None, datetime.now(tz=trigger.timezone))

    print("Scheduler started.")
    print(f"  Schedule : {cron}")
    print(f"  Period   : {period} days")
    print(f"  Next run : {next_run}")
    print("Press Ctrl-C to stop.\n")

    def _shutdown(sig, frame):
        print("\nShutting down scheduler...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    scheduler.start()


if __name__ == "__main__":
    main()

from types import SimpleNamespace

from mercury.orchestrator.orchestrator import MercuryOrchestrator


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs = []

    def add_job(self, func, trigger=None, *args, **kwargs):
        self.jobs.append({"func": func, "trigger": trigger, "kwargs": kwargs})

    def start(self) -> None:
        pass


def _fake_orchestrator(settings):
    orch = MercuryOrchestrator.__new__(MercuryOrchestrator)
    orch.settings = settings
    orch.scheduler = FakeScheduler()
    orch.collector = SimpleNamespace(tick=lambda: None)
    orch.execution = SimpleNamespace(tick=lambda: None)
    orch.news = SimpleNamespace(tick=lambda: None)
    orch.analytics = SimpleNamespace(tick=lambda: None)
    orch.notifications = SimpleNamespace(
        send_daily_report=lambda: None,
        send_weekly_report=lambda: None,
        send_monthly_report=lambda: None,
    )
    orch.hermes = SimpleNamespace(run_daily_analysis=lambda: None)
    return orch


def test_schedule_jobs_registers_weekly_and_monthly_reports(settings):
    orch = _fake_orchestrator(settings)
    orch._schedule_jobs()

    by_id = {job["kwargs"]["id"]: job for job in orch.scheduler.jobs}
    assert {"report_daily", "report_weekly", "report_monthly"} <= by_id.keys()

    weekly = by_id["report_weekly"]
    assert weekly["trigger"] == "cron"
    assert weekly["func"] == orch.notifications.send_weekly_report
    assert weekly["kwargs"]["day_of_week"] == "fri"
    assert weekly["kwargs"]["hour"] == 23
    assert weekly["kwargs"]["minute"] == 55

    monthly = by_id["report_monthly"]
    assert monthly["trigger"] == "cron"
    assert monthly["func"] == orch.notifications.send_monthly_report
    assert monthly["kwargs"]["day"] == "last"
    assert monthly["kwargs"]["hour"] == 23
    assert monthly["kwargs"]["minute"] == 55


def test_parse_report_schedule_daily():
    assert MercuryOrchestrator._parse_report_schedule("23:55") == {
        "hour": 23,
        "minute": 55,
    }


def test_parse_report_schedule_weekly():
    assert MercuryOrchestrator._parse_report_schedule("fri 23:55") == {
        "day_of_week": "fri",
        "hour": 23,
        "minute": 55,
    }


def test_parse_report_schedule_last_day_of_month():
    assert MercuryOrchestrator._parse_report_schedule("last-day 23:55") == {
        "day": "last",
        "hour": 23,
        "minute": 55,
    }

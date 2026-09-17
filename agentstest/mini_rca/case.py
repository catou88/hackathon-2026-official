from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
import re

TZ = timezone(timedelta(hours=8))
FIELDS = {
    "task_1": ("datetime",), "task_2": ("reason",), "task_3": ("component",),
    "task_4": ("datetime", "reason"), "task_5": ("datetime", "component"),
    "task_6": ("component", "reason"), "task_7": ("datetime", "component", "reason"),
}

def epoch(text: str) -> float:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.timestamp()

def local_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S")

@dataclass(frozen=True)
class Case:
    row_id: int
    task: str
    instruction: str
    start: str
    end: str
    failures: int
    fields: tuple[str, ...]

    @classmethod
    def parse(cls, row: dict):
        instruction = row["instruction"]
        m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", instruction)
        times = re.findall(r"\b(\d{1,2}):(\d{2})\b", instruction)
        if not m or len(times) != 2:
            raise ValueError("Could not parse exactly two clock bounds from instruction")
        mon, day, year = m.groups()
        month = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"].index(mon.lower())+1
        lo = datetime(int(year), month, int(day), *map(int, times[0]), tzinfo=TZ)
        hi = lo.replace(hour=int(times[1][0]), minute=int(times[1][1]))
        if hi <= lo:
            hi += timedelta(days=1)
        if hi-lo != timedelta(minutes=30):
            raise ValueError("Expected a 30-minute benchmark window")
        count = 1
        for word, n in [("one",1),("single",1),("two",2),("three",3),("four",4),("1",1),("2",2),("3",3),("4",4)]:
            if re.search(rf"\b{word}\s+failures?\b", instruction, re.I):
                count = n
        if re.search(r"unknown number|number of failures.*unknown", instruction, re.I):
            raise ValueError("Unknown failure count is unsupported")
        task = row.get("task_index", "task_7")
        if task not in FIELDS:
            raise ValueError(f"Unsupported task: {task}")
        return cls(int(row["row_id"]), task, instruction, local_time(lo.timestamp()), local_time(hi.timestamp()), count, FIELDS[task])

    def public(self):
        return asdict(self)

def format_prediction(answers: list[dict], fields: tuple[str,...]) -> str:
    import json
    keys = {"datetime":"root cause occurrence datetime", "component":"root cause component", "reason":"root cause reason"}
    ordered = sorted(answers, key=lambda a:a["datetime"])
    return json.dumps({str(i):{keys[k]:a[k] for k in keys if k in fields} for i,a in enumerate(ordered,1)}, ensure_ascii=False)

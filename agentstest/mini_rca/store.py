"""Whitelisted telemetry relations with explicit types and interruptible DuckDB queries."""
from datetime import datetime, timedelta
from pathlib import Path
import csv
import hashlib
import json
import threading
import time
import duckdb
from .case import TZ, epoch

METRIC = {"timestamp":"DOUBLE", "cmdb_id":"VARCHAR", "kpi_name":"VARCHAR", "value":"DOUBLE"}
SCHEMAS = {name: METRIC for name in ["metric_container", "metric_node", "metric_mesh", "metric_runtime"]}
SCHEMAS.update({
    "metric_service":{"service":"VARCHAR", "timestamp":"DOUBLE", "rr":"DOUBLE", "sr":"DOUBLE", "mrt":"DOUBLE", "count":"DOUBLE"},
    "log_service":{"log_id":"VARCHAR", "timestamp":"DOUBLE", "cmdb_id":"VARCHAR", "log_name":"VARCHAR", "value":"VARCHAR"},
    "log_proxy":{"log_id":"VARCHAR", "timestamp":"DOUBLE", "cmdb_id":"VARCHAR", "log_name":"VARCHAR", "value":"VARCHAR"},
    "trace_span":{"timestamp":"DOUBLE", "cmdb_id":"VARCHAR", "span_id":"VARCHAR", "trace_id":"VARCHAR", "duration":"DOUBLE", "type":"VARCHAR", "status_code":"VARCHAR", "operation_name":"VARCHAR", "parent_span":"VARCHAR"},
})

def literal(value):
    return "'" + str(value).replace("'", "''") + "'"

class TelemetryStore:
    def __init__(self, dataset, out, config):
        self.dataset = Path(dataset).resolve()
        self.out = Path(out).resolve()
        if self.out == self.dataset or self.dataset in self.out.parents:
            raise ValueError("Output must be outside the read-only dataset")
        self.cache = self.out / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        (self.out/'tmp').mkdir(parents=True,exist_ok=True)
        self.config = config
        self.db = duckdb.connect(config={"threads":config.threads,"memory_limit":config.memory_limit,
            "temp_directory":str(self.cache/"spill"),"enable_external_access":True,
            "autoinstall_known_extensions":False,"autoload_known_extensions":False})
        self.deadline = float("inf")
        self.window_tables = {}
        self.global_tables = {}
        self.query_events = []

    def execute(self, sql, params=None, seconds=None, phase='query'):
        remaining = min(seconds or self.config.query_seconds, self.deadline-time.monotonic())
        if remaining <= 0:
            self.query_events.append({'phase':phase,'status':'budget_exhausted','seconds':0,'budget_s':0})
            raise TimeoutError("Data query budget exhausted")
        started=time.monotonic()
        expired=threading.Event()
        def interrupt():
            expired.set()
            self.db.interrupt()
        timer = threading.Timer(remaining, interrupt)
        timer.daemon = True
        timer.start()
        event={'phase':phase,'budget_s':round(remaining,3),'sql_hash':hashlib.sha256(sql.encode()).hexdigest()[:12]}
        try:
            cursor = self.db.execute(sql, params or [])
            names = [d[0] for d in cursor.description]
            rows=[dict(zip(names,r)) for r in cursor.fetchall()]
            event['status']='ok'
            return rows
        except Exception as exc:
            event.update(status='timeout' if expired.is_set() else 'failed',error_type=type(exc).__name__)
            if expired.is_set():
                raise TimeoutError(f'{phase} exceeded query budget ({remaining:.3f}s)') from exc
            raise
        finally:
            timer.cancel()
            timer.join()
            event['seconds']=round(time.monotonic()-started,3)
            self.query_events.append(event)

    def files(self, name, start, end):
        if name not in SCHEMAS:
            raise ValueError("Unknown telemetry table")
        lo, hi = epoch(start), epoch(end)
        if hi <= lo or hi-lo > 172800:
            raise ValueError("Window must be positive and at most two days")
        day = datetime.fromtimestamp(lo,TZ).date()
        last = datetime.fromtimestamp(hi-0.001,TZ).date()
        found = []
        kind = name.split("_")[0]
        while day <= last:
            p = self.dataset/"telemetry"/day.strftime("%Y_%m_%d")/kind/(name+".csv")
            if p.exists():
                p = p.resolve()
                if self.dataset not in p.parents:
                    raise ValueError("Telemetry path escapes dataset")
                found.append(p)
            day += timedelta(days=1)
        if not found:
            raise FileNotFoundError(f"No {name} for requested dates")
        return found

    def fingerprint(self, p):
        stat = p.stat()
        return hashlib.sha256(f"v1|{p}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()[:24]

    def relation(self, name, start, end):
        parts=[]
        for p in self.files(name,start,end):
            cache_file=self.cache/(self.fingerprint(p)+".parquet")
            if cache_file.exists():
                reader=f"read_parquet({literal(cache_file)})"
            else:
                with p.open(newline='',encoding='utf-8-sig') as f:
                    if next(csv.reader(f)) != list(SCHEMAS[name]):
                        raise ValueError(f"Unexpected schema in {p.name}")
                schema="{"+",".join(f"{literal(k)}:{literal(v)}" for k,v in SCHEMAS[name].items())+"}"
                reader=f"read_csv({literal(p)}, header=true, columns={schema}, auto_detect=false, parallel=false)"
            parts.append(f"SELECT *, {literal(p.relative_to(self.dataset).as_posix())} AS source_file FROM {reader}")
        return "("+" UNION ALL ".join(parts)+")"

    def metric_relation(self, name, start, end):
        rel = self.relation(name,start,end)
        if name != "metric_service":
            return rel
        return f"(SELECT timestamp, service AS cmdb_id, kpi_name, value, source_file FROM {rel} UNPIVOT (value FOR kpi_name IN (rr,sr,mrt,count)))"

    def window(self, name, start, end):
        rel=self.relation(name,start,end)
        key=hashlib.sha256(f"{rel}|{start}|{end}".encode()).hexdigest()[:20]
        if key in self.window_tables:
            return self.window_tables[key]
        # Limit persistent window caches; daily metric statistics remain reusable.
        if len(self.window_tables)>=8:
            old=next(iter(self.window_tables))
            self.execute(f"DROP TABLE {self.window_tables.pop(old)}")
        table="w_"+key
        divisor=1000 if name=="trace_span" else 1
        self.execute(f"CREATE TEMP TABLE {table} AS SELECT *, timestamp/{divisor} AS ts FROM {rel} WHERE timestamp >= ? AND timestamp < ?",[epoch(start)*divisor,epoch(end)*divisor],phase='materialize_'+name)
        self.window_tables[key]=table
        return table

    def global_stats(self, name, start, end):
        rel=self.metric_relation(name,start,end)
        key=hashlib.sha256(rel.encode()).hexdigest()[:20]
        if key not in self.global_tables:
            table="g_"+key
            self.execute(f"""CREATE TEMP TABLE {table} AS
                SELECT cmdb_id,kpi_name,quantile_cont(value,0.5) baseline,
                quantile_cont(value,0.05) p05,quantile_cont(value,0.95) p95,count(*) baseline_n
                FROM {rel} WHERE isfinite(value) GROUP BY cmdb_id,kpi_name""")
            self.global_tables[key]=table
        return self.global_tables[key]

    def inventory(self):
        rows=[]
        for name in SCHEMAS:
            for p in sorted((self.dataset/"telemetry").glob(f"*/{name.split('_')[0]}/{name}.csv")):
                if self.dataset not in p.resolve().parents:
                    raise ValueError("Telemetry path escapes dataset")
                rows.append({"table":name,"file":p.relative_to(self.dataset).as_posix(),"bytes":p.stat().st_size,"columns":SCHEMAS[name],"timestamp_unit":"ms" if name=="trace_span" else "s"})
        return rows

    def prepare(self, names, start, end, seconds=300):
        outputs=[]
        for name in names:
            for p in self.files(name,start,end):
                target=self.cache/(self.fingerprint(p)+".parquet")
                if not target.exists():
                    day=p.parent.parent.name.replace('_','-')
                    next_day=(datetime.fromisoformat(day)+timedelta(days=1)).strftime('%Y-%m-%d')
                    rel=self.relation(name,day+" 00:00:00",next_day+" 00:00:00")
                    tmp=target.with_suffix('.tmp.parquet')
                    self.execute(f"COPY (SELECT * EXCLUDE (source_file) FROM {rel} ORDER BY timestamp) TO {literal(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD)",seconds=seconds)
                    tmp.replace(target)
                outputs.append(str(target))
        return outputs

    def close(self):
        self.db.close()

"""Public, schema-validated tools. SQL is internal; callers cannot supply code or paths."""
from pathlib import Path
import hashlib
import json
import re
import time
from jsonschema import validate
from .case import epoch, local_time
from .store import SCHEMAS

def component_name(cmdb):
    if re.match(r"^node-[^.]+\.",cmdb):
        return cmdb.split('.',1)[1]
    return re.sub(r"-(grpc|http)$","",cmdb)

def service_name(cmdb):
    return re.sub(r"-\d+$","",component_name(cmdb))

def status_success(code):
    s=str(code).strip().lower()
    return s in {"0","ok"} or (s.isdigit() and 200<=int(s)<400)

class Tools:
    def __init__(self, store, config):
        self.store=store
        self.config=config
        self.ledger=[]
        self.on_result=None
        root=Path(__file__).resolve().parents[1]
        self.knowledge=json.loads((root/'knowledge/domain.json').read_text(encoding='utf-8'))
        self.experiences=json.loads((root/'knowledge/experiences.json').read_text(encoding='utf-8'))

    def call(self, name, arguments=None):
        arguments=arguments or {}
        schema=next((t['function'] for t in TOOL_SCHEMAS if t['function']['name']==name),None)
        if schema is None:
            raise ValueError("Tool is not allowed")
        validate(arguments,schema['parameters'])
        started=time.monotonic()
        query_start=len(self.store.query_events)
        try:
            data=getattr(self,name)(**arguments)
            result={"ok":True,"data":data}
        except Exception as e:
            result={"ok":False,"error":f"{type(e).__name__}: {str(e)[:600]}"}
        payload=json.dumps({"tool":name,"arguments":arguments,**result},ensure_ascii=False,default=str,allow_nan=False)
        # Stable within the same tool result; evidence IDs never claim a diagnosis.
        result={"evidence_id":"ev_"+hashlib.sha256(payload.encode()).hexdigest()[:12],"tool":name,"arguments":arguments,**result,"seconds":round(time.monotonic()-started,3)}
        self.ledger.append(result)
        result['queries']=self.store.query_events[query_start:]
        if self.on_result:self.on_result()
        return result

    def describe_dataset(self):
        return {"files":self.store.inventory(),"timezone":"UTC+8","answer_files_accessible":False,
            "duration_unit":"unverified: keep trace duration in original units"}

    def retrieve_knowledge(self, query, limit=4):
        words=set(re.findall(r"[a-z0-9]+",query.lower()))
        scored=[(len(words & set(re.findall(r"[a-z0-9]+",json.dumps(card).lower()))),card) for card in self.knowledge]
        return [c for score,c in sorted(scored,key=lambda x:-x[0]) if score>0][:limit]

    def retrieve_experience(self, query, limit=2):
        if not self.config.experiences:
            return {"enabled":False,"cards":[],"note":"Optional layer disabled; never imports dev answers."}
        words=set(re.findall(r"[a-z]+",query.lower()))
        cards=sorted(self.experiences,key=lambda c:-len(words & set(re.findall(r"[a-z]+",json.dumps(c).lower()))))
        return {"enabled":True,"cards":cards[:limit],"note":"Generic investigation patterns, not solved benchmark incidents."}

    def list_entities(self, start, end, source="metric_container"):
        rel=self.store.metric_relation(source,start,end) if source.startswith('metric_') else self.store.relation(source,start,end)
        rows=self.store.execute(f"SELECT DISTINCT cmdb_id FROM {rel} ORDER BY cmdb_id LIMIT 1000")
        entities=[]; allowed=set(); scopes={}
        for row in rows:
            cmdb=row['cmdb_id']; component=component_name(cmdb)
            node=cmdb.split('.')[0] if re.match(r'^node-[^.]+\.',cmdb) else None
            scope='node' if source=='metric_node' else ('service' if source=='metric_service' else ('unknown' if source=='metric_mesh' else 'container'))
            service=service_name(cmdb) if scope!='node' else None
            entities.append({"cmdb_id":cmdb,"component":component,"node":node,"service":service,"scope":scope})
            if scope in {'node','container'}:
                allowed.add(component); scopes[component]=scope
            if node:
                allowed.add(node); scopes[node]='node'
        return {"entities":entities,"candidate_names":sorted(allowed),"component_scopes":scopes,"truncated_at":1000,
            "note":"Only observed node/container names are answer candidates. Derived service names are context, not interchangeable with instance IDs. Trace/log IDs retain their recorded instance scope."}

    def metric_anomalies(self, start, end, sources=None, top_k=12):
        sources=sources or ['metric_container','metric_node','metric_service']
        found=[]; gaps=[]
        for name in sources:
            try:
                stats=self.store.global_stats(name,start,end)
                rel=self.store.metric_relation(name,start,end)
                rows=self.store.execute(f"""WITH series AS (
                    SELECT r.*,g.baseline,g.p05,g.p95,g.baseline_n,
                    greatest(abs(g.baseline)*0.05,g.p95-g.p05,0.000001) AS scale
                    FROM {rel} r JOIN {stats} g USING(cmdb_id,kpi_name)
                    WHERE timestamp>=? AND timestamp<? AND isfinite(value)
                    AND NOT regexp_matches(kpi_name,'spec_|start_time|last_seen|_limit|_max$|_total$')
                ), marked AS (
                    SELECT *,CASE WHEN value>p95 THEN (value-p95)/scale WHEN value<p05 THEN (p05-value)/scale ELSE 0 END score FROM series
                ), sequence AS (
                    SELECT *, lag(score) OVER (PARTITION BY cmdb_id,kpi_name ORDER BY timestamp) previous_score,
                    lag(timestamp) OVER (PARTITION BY cmdb_id,kpi_name ORDER BY timestamp) previous_ts FROM marked
                ) SELECT cmdb_id,kpi_name,any_value(source_file) source_file,
                    any_value(baseline) baseline,any_value(p05) p05,any_value(p95) p95,any_value(baseline_n) baseline_n,
                    count(*) window_n, min(value) window_min,max(value) window_max,max(score) score,
                    count(*) FILTER (WHERE score>0.5) anomalous_samples,
                    count(*) FILTER (WHERE score>0.5 AND previous_score>0.5 AND timestamp-previous_ts BETWEEN 1 AND 90) adjacent_anomalies,
                    min(timestamp) FILTER (WHERE score>0.5) first_anomaly_ts
                    FROM sequence GROUP BY cmdb_id,kpi_name HAVING max(score)>0.5
                    ORDER BY (adjacent_anomalies>0) DESC,score DESC LIMIT ?""",[epoch(start),epoch(end),top_k])
                for row in rows:
                    row.update(source=name,component=component_name(row['cmdb_id']),first_anomaly=local_time(row.pop('first_anomaly_ts')))
                found.extend(rows)
            except Exception as e:
                gaps.append({"source":name,"error":type(e).__name__+": "+str(e)[:160]})
        # Diversity: at most two KPIs for one component, preserving multiple causes.
        chosen=[]; counts={}
        for row in sorted(found,key=lambda x:(x['adjacent_anomalies']>0,x['score']),reverse=True):
            c=row['component']
            if counts.get(c,0)<2:
                chosen.append(row); counts[c]=counts.get(c,0)+1
            if len(chosen)>=top_k: break
        return {"candidates":chosen,"gaps":gaps,"baseline":"Full supplied day(s), per component/KPI, before time filtering; may contain other injected faults.",
            "caveats":["Threshold excursions are candidates, not proof of root cause.","Raw metric semantics may include counters, gauges and sentinels; inspect a series before interpreting resource load.","Earliest sampled anomaly is not an exact injection timestamp.","Single spikes retained as lower-priority candidates for node CPU spike."]}

    def metric_series(self, start, end, source, cmdb_id, kpi_name, limit=180):
        rel=self.store.metric_relation(source,start,end)
        rows=self.store.execute(f"""WITH s AS (SELECT timestamp,value,source_file,
            value-lag(value) OVER (ORDER BY timestamp) delta,
            timestamp-lag(timestamp) OVER (ORDER BY timestamp) delta_seconds
            FROM {rel} WHERE cmdb_id=? AND kpi_name=?)
            SELECT * FROM s WHERE timestamp>=? AND timestamp<? ORDER BY timestamp LIMIT ?""",
            [cmdb_id,kpi_name,epoch(start),epoch(end),limit+1])
        return {"rows":rows[:limit],"truncated":len(rows)>limit,"cmdb_id":cmdb_id,"kpi_name":kpi_name,
            "note":"Deltas are descriptive, not automatically rates. Confirm metric units and counter resets."}

    def log_search(self, start, end, source="log_service", components=None, keywords=None, limit=12):
        table=self.store.window(source,start,end)
        where=[]; args=[]
        if components:
            where.append('cmdb_id IN ('+','.join('?' for _ in components)+')'); args.extend(components)
        if keywords:
            where.append('('+' OR '.join('contains(lower(value),?)' for _ in keywords)+')'); args.extend(x.lower() for x in keywords)
        predicate=' AND '.join(where) or 'TRUE'
        rows=self.store.execute(f"""WITH normalized AS (
            SELECT *,regexp_replace(regexp_replace(value,'[0-9a-f]{{8}}-[0-9a-f-]{{20,}}','<id>','g'),'[0-9]+','<n>','g') AS log_template
            FROM {table} WHERE {predicate}
            ) SELECT cmdb_id,log_name,left(log_template,350) AS log_template,count(*) occurrences,min(ts) first_ts,max(ts) last_ts,
            arg_min(struct_pack(log_id:=log_id,"timestamp":=timestamp,"value":=left(value,650),source_file:=source_file),ts) AS "sample",
            max(CASE WHEN regexp_matches(lower(value),'error|exception|timeout|failed|refused|unavailable') THEN 1 ELSE 0 END) error_hint
            FROM normalized GROUP BY cmdb_id,log_name,log_template ORDER BY error_hint DESC,occurrences DESC LIMIT ?""",args+[limit])
        return {"groups":rows,"scope":{"source":source,"start":start,"end":end},
            "note":"Template strings are lossy; cited sample is original text, capped at 650 characters. Counts cover the requested filter. Error keywords are hints; normal info logs may matter."}

    def trace_summary(self, start, end, components=None, limit=12):
        table=self.store.window('trace_span',start,end)
        where='TRUE'; args=[]
        if components:
            where='cmdb_id IN ('+','.join('?' for _ in components)+')'; args.extend(components)
        rows=self.store.execute(f"""SELECT cmdb_id,operation_name,count(*) spans,quantile_cont(duration,0.5) duration_p50_raw,
            quantile_cont(duration,0.95) duration_p95_raw,max(duration) duration_max_raw,
            count(*) FILTER (WHERE coalesce(status_code,'') NOT IN ('0','Ok','OK') AND NOT coalesce(try_cast(status_code AS INTEGER) BETWEEN 200 AND 399,false)) nonstandard_status_count,
            arg_max(struct_pack(trace_id:=trace_id,span_id:=span_id,"timestamp":=timestamp,status_code:=status_code,source_file:=source_file),duration) slow_sample
            FROM {table} WHERE {where} GROUP BY cmdb_id,operation_name ORDER BY duration_p95_raw DESC LIMIT ?""",args+[limit])
        return {"operations":rows,"duration_unit":"raw, unverified", "note":"Status conventions vary by instrumentation. Nonstandard status is a lead, not a universally valid error classification."}

    def trace_edges(self, start, end, components=None, limit=12):
        table=self.store.window('trace_span',start,end)
        where="c.parent_span IS NOT NULL AND c.parent_span<>''"; args=[]
        if components:
            placeholders=','.join('?' for _ in components)
            where+=f' AND (c.cmdb_id IN ({placeholders}) OR p.cmdb_id IN ({placeholders}))'
            args.extend(components); args.extend(components)
        rows=self.store.execute(f"""WITH spans AS (
            SELECT * FROM {table} QUALIFY row_number() OVER (PARTITION BY trace_id,span_id ORDER BY timestamp)=1
            ) SELECT p.cmdb_id parent_component,c.cmdb_id child_component,p.cmdb_id<>c.cmdb_id AS cross_component,count(*) matched_spans,
            quantile_cont(c.timestamp-p.timestamp,0.5) start_gap_p50_ms,
            quantile_cont(c.timestamp-p.timestamp,0.95) start_gap_p95_ms,
            count(*) FILTER (WHERE c.timestamp<p.timestamp) negative_gaps,
            arg_max(struct_pack(trace_id:=c.trace_id,parent_span:=p.span_id,child_span:=c.span_id,parent_timestamp:=p.timestamp,child_timestamp:=c.timestamp,source_file:=c.source_file),c.timestamp-p.timestamp) AS "sample"
            FROM spans c JOIN spans p ON c.trace_id=p.trace_id AND c.parent_span=p.span_id
            WHERE {where} GROUP BY p.cmdb_id,c.cmdb_id ORDER BY cross_component DESC,start_gap_p95_ms DESC LIMIT ?""",args+[limit])
        coverage=self.store.execute(f"""SELECT count(*) spans,count(*) FILTER (WHERE parent_span IS NULL OR parent_span='') root_or_missing_parent FROM {table}""")[0]
        return {"edges":rows,"coverage":coverage,"note":"Parent-child start gaps include scheduling/processing and clock skew; they are not measured network latency. Parents outside this window are excluded. Duplicate (trace_id,span_id) records collapse to the earliest for this join only."}

STR={"type":"string","minLength":1,"maxLength":300}
TIME={"type":"string","pattern":r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2})?$"}
WINDOW={"start":TIME,"end":TIME}
COMPONENTS={"type":"array","items":STR,"maxItems":12,"uniqueItems":True}
METRICS={"type":"string","enum":[x for x in SCHEMAS if x.startswith('metric_')]}
def tool(name,description,properties,required):
    return {"type":"function","function":{"name":name,"description":description,"parameters":{"type":"object","properties":properties,"required":required,"additionalProperties":False}}}
def limit(n=40):
    return {"type":"integer","minimum":1,"maximum":n}

TOOL_SCHEMAS=[
    tool('describe_dataset','List available telemetry files, schemas and units; no answer-file access.',{},[]),
    tool('retrieve_knowledge','Retrieve small domain cards by lexical overlap.',{'query':STR,'limit':limit(6)},['query']),
    tool('retrieve_experience','Optional generic investigation patterns; disabled by default.',{'query':STR,'limit':limit(3)},['query']),
    tool('list_entities','Discover actual node/container names; service names are context only. Trace/log sources support recovery when metrics are unavailable.',{**WINDOW,'source':{'type':'string','enum':list(SCHEMAS)}},['start','end']),
    tool('metric_anomalies','Find per-component/KPI excursions against full-day thresholds before window filtering. Not a causal diagnosis.',{**WINDOW,'sources':{'type':'array','items':METRICS,'minItems':1,'maxItems':5,'uniqueItems':True},'top_k':limit(30)},['start','end']),
    tool('metric_series','Read exact series and adjacent deltas for one known KPI and CMDB ID.',{**WINDOW,'source':METRICS,'cmdb_id':STR,'kpi_name':STR,'limit':limit(240)},['start','end','source','cmdb_id','kpi_name']),
    tool('log_search','Search filtered logs, count templates, retain original sample and IDs. Empty keywords includes info logs.',{**WINDOW,'source':{'type':'string','enum':['log_service','log_proxy']},'components':COMPONENTS,'keywords':{'type':'array','items':STR,'maxItems':8},'limit':limit(25)},['start','end']),
    tool('trace_summary','Summarize span durations in raw units and status patterns; timestamp is milliseconds.',{**WINDOW,'components':COMPONENTS,'limit':limit(25)},['start','end']),
    tool('trace_edges','Join parent/child spans on trace_id and span_id; return component edges and start gaps in ms.',{**WINDOW,'components':COMPONENTS,'limit':limit(25)},['start','end']),
]

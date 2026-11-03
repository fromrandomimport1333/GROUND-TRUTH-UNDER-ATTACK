"""Streaming compact CSV and full v1 FeatureWindow exports from identical objects."""
import csv
import gzip
import json
from contextlib import ExitStack


class FeatureExport:
    def __init__(self, directory, compresslevel=9):
        self.directory = directory
        self.stack = ExitStack()
        self.full = self.stack.enter_context(gzip.open(directory/'windows.jsonl.gz','wt',compresslevel=compresslevel,encoding='utf-8',newline='\n'))
        self.compact = self.stack.enter_context(gzip.open(directory/'compact.csv.gz','wt',compresslevel=compresslevel,encoding='utf-8',newline=''))
        self.lineage = self.stack.enter_context(gzip.open(directory/'lineage.jsonl.gz','wt',compresslevel=compresslevel,encoding='utf-8',newline='\n'))
        self.writer = None
        self.rows = self.full_bytes = self.compact_bytes = 0
        self.missing = {}
        self.schema = None

    def write(self, window, row, lineage):
        if self.writer is None:
            self.writer = csv.DictWriter(self.compact,fieldnames=list(row),lineterminator='\n')
            self.writer.writeheader()
            units = {f.name:f.unit for g in (window.temporal_features,window.spatial_features) for f in g.values}
            self.schema = {'format':'CSV gzip UTF-8','null':'empty field','ordering':['timestamp','cell_id'],
                'columns':[{ 'name':k,'type':('float64' if k in units or k.endswith('__raw_flag_percent') else
                    'boolean' if k.endswith('__eligible') else 'string'),
                    'unit':units.get(k,'%' if k.endswith('__raw_flag_percent') else None)} for k in row]}
        self.writer.writerow(row)
        text = window.model_dump_json()+'\n'
        self.full.write(text)
        self.full_bytes += len(text.encode('utf-8'))
        self.lineage.write(json.dumps(compact_lineage(lineage),separators=(',',':'),allow_nan=False)+'\n')
        self.rows += 1
        for name,value in row.items():
            if value is None:
                self.missing[name] = self.missing.get(name,0)+1

    def close(self):
        self.stack.close()
        (self.directory/'compact-schema.json').write_text(json.dumps(self.schema,indent=2),encoding='utf-8')


def compact_lineage(lineage):
    """Lossless references by sensor/quantity/hour range into the complete journal.

    Avoid repeating 169 random SHA256 IDs per variable in every output row.
    Source observations are unique by sensor/quantity/hour (enforced by engine).
    Missing offsets identify slots with no observation record.
    """
    history = {}
    for name, ids in lineage['history'].items():
        if isinstance(ids, dict):
            history[name] = ids
        else:
            history[name] = dict(slots=len(ids), first_id=ids[0] if ids else None,
                                 last_id=ids[-1] if ids else None,
                                 missing_offsets=[i for i,v in enumerate(ids) if v is None])
    return {**lineage,'history':history,'history_reference_rule':('same_cell_variable_1800s_range_ending_at_timestamp_v1' if lineage.get('interval_seconds')==1800 else 'same_cell_variable_hourly_range_ending_at_timestamp_v1')}
